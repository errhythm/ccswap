"""Tool settings persisted at ``<backup_root>/settings.json``.

One versioned JSON file for user-tunable claude-swap preferences, written
atomically with the backup dir's 0600/0700 modes. v1 carries the
``autoswitch``, ``ui``, ``run``, ``updates`` and ``credentials`` sections, plus
``claude`` (the statusline saved by ``claude_integration``); other
sections can be added additively.
Unknown keys (future fields, other tools' experiments) survive a round trip.

Reading is forgiving — a missing or corrupt file yields defaults with a logged
warning, never a crash — so a bad hand edit degrades to default behavior.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from claude_swap.exceptions import ConfigError
from claude_swap.fsutil import atomic_write_text, publish_target

SETTINGS_SCHEMA_VERSION = 1
SETTINGS_FILENAME = "settings.json"

_logger = logging.getLogger("claude-swap")


@dataclass(frozen=True)
class AutoSwitchSettings:
    """Policy knobs for the auto-switch engine (``cswap auto``).

    ``threshold`` is the default gate: a window at or above it makes the
    engine look for a better account. 90 rather than 95 leaves margin for
    the macOS ~30s Keychain pickup tail and for heavy subagent turns burning
    past the mark before a swap lands. Optional per-gate overrides
    (``threshold_5h``, ``threshold_7d``, ``model_thresholds``) replace that
    number for one window only — any gate tripping switches, so Fable can
    leave earlier than the 5-hour window. A proactive candidate must itself
    sit below every gate (never land somewhere that re-triggers next tick)
    and beat the active account by at least ``hysteresis_pct``, so two
    accounts hovering at the line never ping-pong while a strictly better
    account is always taken.
    """

    threshold: float = 90.0
    # Upstream's switch line for the 7-day window and every per-model window.
    # Here an alias below the specific gates: threshold_7d / model_thresholds
    # win where set. None = ``threshold`` (see ``gate_thresholds``).
    weekly_threshold: float | None = None
    interval_seconds: float = 60.0
    cooldown_seconds: float = 300.0
    hysteresis_pct: float = 10.0
    strategy: str = "best"  # "best" (most headroom) or "consume-first" (soonest weekly reset)
    include_api_key_accounts: bool = False
    unhealthy_ticks: int = 3
    # Comma-separated model display name(s) (e.g. "Fable" or "Fable,Opus"),
    # or "all" for every scoped window an account reports. Each named model's
    # per-model weekly limit is folded into the binding window, so the engine
    # switches off an account whose model quota is exhausted even while its
    # 5h/7d windows still have headroom. None = account-wide 5h/7d only
    # (default).
    model: str | None = None
    # Which of Claude's two account-wide windows bind the switch decision:
    # "both" (default, unchanged), "5h" (ignore the weekly window entirely —
    # for someone happy to run their weekly quota all the way down), or "7d"
    # (ignore the rolling session window, switch only on the weekly one).
    windows: str = "both"
    # Optional per-gate thresholds. None inherits ``threshold``. Any one gate
    # at or over its own number triggers a switch, so the session window, the
    # weekly window, and a model like Fable can each have their own wall.
    threshold_5h: float | None = None
    threshold_7d: float | None = None
    # "Fable=40" or "Fable=40,Opus=70". Named models become gates even when
    # absent from ``model``; a model listed in ``model`` without an entry
    # here still uses ``threshold``.
    model_thresholds: str | None = None
    # Real, quota-consuming Haiku requests; deliberately opt-in.
    warmup_five_hour: bool = False


@dataclass(frozen=True)
class UiSettings:
    """Appearance preferences (``ui`` section). ``theme`` selects the TUI/CLI
    color theme; ``auto`` follows terminal-background detection. ``mask``
    redacts account emails and organization names in the TUI, the Claude Code mod
    and the statusline."""

    theme: str = "auto"
    view: str = "combined"
    mask: bool = False
    star_prompt: bool = True


@dataclass(frozen=True)
class CredentialSettings:
    """Credential storage preferences (``credentials`` section). ``backend``
    selects automatic platform routing or an explicit file-only backend."""

    backend: str = "auto"


@dataclass(frozen=True)
class ClaudeIntegrationSettings:
    """Claude Code integrations (``claude`` section): action keys whose value
    is read live from Claude Code (see ``claude_integration``), never stored.
    The section itself only holds ``previousStatusline``."""

    statusline: bool = False
    mod: bool = False


@dataclass(frozen=True)
class RunSettings:
    """Defaults for ``cswap run`` (``run`` section); its flags override them."""

    share_history: bool = False


@dataclass(frozen=True)
class UpdateSettings:
    """Update notification (``updates`` section). ``check`` turns the daily
    PyPI lookup behind the "newer version available" hint on or off;
    ``ccswap upgrade`` works either way."""

    check: bool = True


_SECTION_DEFAULT_SOURCES = {
    "autoswitch": AutoSwitchSettings,
    "ui": UiSettings,
    "credentials": CredentialSettings,
    "claude": ClaudeIntegrationSettings,
    "run": RunSettings,
    "updates": UpdateSettings,
}


@dataclass(frozen=True)
class SettingSpec:
    """Metadata for one user-tunable settings.json key.

    Single source of truth for bounds/choices: both the lenient clamp on load
    (`_clamped`) and the strict validation in `cswap config set`
    (`parse_setting_value`) read from here, so the two can't drift.
    """

    section: str  # top-level JSON section ("autoswitch", "ui", "updates")
    json_key: str  # camelCase key inside the section
    field: str  # snake_case AutoSwitchSettings field
    kind: str  # "float" | "optional_float" | "int" | "bool" | "choice" | "string" | "model_thresholds"
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()
    help: str = ""

    @property
    def dotted(self) -> str:
        return f"{self.section}.{self.json_key}"

    @property
    def default(self):
        return getattr(_SECTION_DEFAULT_SOURCES[self.section](), self.field)


# settings.json uses camelCase (matching the repo's other JSON artifacts);
# dataclass fields stay snake_case.
SETTING_SPECS: dict[str, SettingSpec] = {
    spec.dotted: spec
    for spec in (
        SettingSpec(
            "autoswitch", "threshold", "threshold", "float", 50.0, 99.9,
            help="Switch when the binding 5h/7d window reaches this pct",
        ),
        SettingSpec(
            "autoswitch", "weeklyThreshold", "weekly_threshold", "optional_float", 50.0, 99.9,
            help="7d and per-model gate below threshold7d/modelThresholds; unset uses threshold",
        ),
        SettingSpec(
            "autoswitch", "intervalSeconds", "interval_seconds", "float", 15.0, 3600.0,
            help="Poll interval for the ccswap auto loop, in seconds",
        ),
        SettingSpec(
            "autoswitch", "warmupFiveHour", "warmup_five_hour", "bool",
            help="Start idle Claude 5h windows with a minimal Haiku request (uses quota)",
        ),
        SettingSpec(
            "autoswitch", "cooldownSeconds", "cooldown_seconds", "float", 0.0, 86400.0,
            help="Minimum seconds between proactive switches",
        ),
        SettingSpec(
            "autoswitch", "hysteresisPct", "hysteresis_pct", "float", 0.0, 50.0,
            help="A target must beat the active account by this many pct",
        ),
        SettingSpec(
            "autoswitch", "strategy", "strategy", "choice",
            choices=("best", "consume-first"),
            help="How auto-switch picks the target account",
        ),
        SettingSpec(
            "autoswitch", "includeApiKeyAccounts", "include_api_key_accounts", "bool",
            help="Allow rotating onto managed API-key accounts (bill per token)",
        ),
        SettingSpec(
            "autoswitch", "unhealthyTicks", "unhealthy_ticks", "int", 1, 100,
            help="Consecutive failed polls before an account is unhealthy",
        ),
        SettingSpec(
            "autoswitch", "model", "model", "string",
            help="Also switch on these models' weekly limits (e.g. Fable, Fable,Opus, or all)",
        ),
        SettingSpec(
            "autoswitch", "windows", "windows", "choice",
            choices=("both", "5h", "7d"),
            help="Which account-wide window(s) bind the switch decision",
        ),
        SettingSpec(
            "autoswitch", "threshold5h", "threshold_5h", "optional_float", 1.0, 99.9,
            help="5h gate; unset uses autoswitch.threshold. Any gate trips a switch",
        ),
        SettingSpec(
            "autoswitch", "threshold7d", "threshold_7d", "optional_float", 1.0, 99.9,
            help="Claude 7d and Codex Weekly gate; unset uses autoswitch.threshold",
        ),
        SettingSpec(
            "autoswitch", "modelThresholds", "model_thresholds", "model_thresholds",
            help="Per-model gates, e.g. Fable=40 or Fable=40,Opus=70 (each 1-99.9)",
        ),
        SettingSpec(
            "ui", "theme", "theme", "choice", choices=("dark", "light", "auto"),
            help="Color theme; auto follows the terminal background",
        ),
        SettingSpec(
            "ui", "view", "view", "choice",
            choices=("combined", "claude", "codex"),
            help="Dashboard view: both providers, or only one",
        ),
        SettingSpec(
            "ui", "mask", "mask", "bool",
            help="Mask account emails and organization names in the TUI, Claude Code mod and statusline",
        ),
        SettingSpec(
            "ui", "starPrompt", "star_prompt", "bool",
            help="Ask for a GitHub star after a switch off a nearly exhausted account",
        ),
        SettingSpec(
            "credentials", "backend", "backend", "choice",
            choices=("auto", "file"),
            help=(
                "macOS: file never touches the Keychain (for headless/SSH-only "
                "machines); auto detects it"
            ),
        ),
        SettingSpec(
            "claude", "statusline", "statusline", "bool",
            help="ccswap statusline in Claude Code (off restores yours)",
        ),
        SettingSpec(
            "claude", "mod", "mod", "bool",
            help="ccswap mod (Claude Code plugin ccswap@ccswap)",
        ),
        SettingSpec(
            "run", "shareHistory", "share_history", "bool",
            help="ccswap run shares chat history unless --no-share-history",
        ),
        SettingSpec(
            "updates", "check", "check", "bool",
            help="Check PyPI once a day and print a hint when a newer release exists",
        ),
    )
}

# Setting these runs an install/uninstall instead of writing settings.json;
# their value is whatever Claude Code currently has.
ACTION_KEYS = frozenset({"claude.statusline", "claude.mod"})

_AUTOSWITCH_KEYS: dict[str, str] = {
    spec.field: spec.json_key
    for spec in SETTING_SPECS.values()
    if spec.section == "autoswitch"
}


def settings_path(backup_root: Path) -> Path:
    return backup_root / SETTINGS_FILENAME


def parse_model_names(value: str | None) -> tuple[str, ...]:
    """Split a comma-separated model list, trimmed and case-insensitively
    deduped (first spelling wins). Shared by the auto engine and the manual
    switch strategies so both read ``autoswitch.model`` identically."""
    if not value:
        return ()
    seen: dict[str, str] = {}
    for part in value.split(","):
        name = part.strip()
        if name and name.lower() not in seen:
            seen[name.lower()] = name
    return tuple(seen.values())


def parse_window_selection(value: str | None) -> tuple[str, ...]:
    """``autoswitch.windows`` ("both"/"5h"/"7d") as the tuple ``oauth.
    relevant_windows``'s ``account_windows`` filter expects. An unrecognized
    value (a hand-edited settings.json, an older/newer schema) degrades to
    "both" rather than silently binding on neither window."""
    if value == "5h":
        return ("5h",)
    if value == "7d":
        return ("7d",)
    return ("5h", "7d")


def _pct_bounds() -> tuple[float, float]:
    """Per-gate percentage bounds. Wider than ``autoswitch.threshold``
    (floor 50) so a precious window such as Fable can trip earlier."""
    spec = SETTING_SPECS["autoswitch.threshold5h"]
    return float(spec.lo), float(spec.hi)


def _format_pct(pct: float) -> str:
    return str(int(pct)) if float(pct).is_integer() else str(pct)


def parse_model_thresholds(value: str | None) -> tuple[tuple[str, float], ...]:
    """Lenient ``"Fable=40, Opus=70"`` parse for load time.

    Blank, malformed, and out-of-range pairs are dropped. Names are
    case-insensitively deduped (first spelling wins). ``None`` and
    non-strings yield nothing, so a garbage settings.json value disables
    the extra gates instead of crashing ``ccswap auto``.
    """
    if not isinstance(value, str) or not value.strip():
        return ()
    lo, hi = _pct_bounds()
    seen: dict[str, tuple[str, float]] = {}
    for part in value.split(","):
        if "=" not in part:
            continue
        name, raw = part.split("=", 1)
        name = name.strip()
        if not name:
            continue
        try:
            pct = float(raw.strip())
        except ValueError:
            continue
        if not lo <= pct <= hi:
            continue
        key = name.lower()
        if key not in seen:
            seen[key] = (name, pct)
    return tuple(seen.values())


def format_model_thresholds(pairs: tuple[tuple[str, float], ...]) -> str:
    return ",".join(f"{name}={_format_pct(pct)}" for name, pct in pairs)


def with_model_threshold(
    value: str | None, name: str, pct: float | None
) -> str | None:
    """``modelThresholds`` with one model's gate set, or removed when
    ``pct`` is None. Other models stay, in their original order. None when
    nothing remains."""
    kept = [
        (model, threshold)
        for model, threshold in parse_model_thresholds(value)
        if model.lower() != name.lower()
    ]
    if pct is not None:
        kept.append((name, float(pct)))
    if not kept:
        return None
    return format_model_thresholds(tuple(kept))


def parse_model_thresholds_strict(raw_value: str) -> str:
    """CLI parse: every pair must be ``Name=PCT`` inside the threshold band.

    Returns the canonical ``Fable=40,Opus=70`` spelling.
    """
    value = raw_value.strip()
    if not value:
        raise ConfigError(
            "autoswitch.modelThresholds expects Name=PCT pairs (e.g. Fable=40); "
            "use 'ccswap config unset autoswitch.modelThresholds' to clear it"
        )
    lo, hi = _pct_bounds()
    parsed: list[tuple[str, float]] = []
    seen: set[str] = set()
    for part in value.split(","):
        piece = part.strip()
        if not piece:
            continue
        if "=" not in piece:
            raise ConfigError(
                "autoswitch.modelThresholds expects Name=PCT pairs "
                f"(e.g. Fable=40), got '{piece}'"
            )
        name, raw = piece.split("=", 1)
        name = name.strip()
        if not name:
            raise ConfigError(
                "autoswitch.modelThresholds expects Name=PCT pairs "
                f"(e.g. Fable=40), got '{piece}'"
            )
        try:
            pct = float(raw.strip())
        except ValueError:
            raise ConfigError(
                "autoswitch.modelThresholds expects a number after '=', "
                f"got '{piece}'"
            ) from None
        if not lo <= pct <= hi:
            raise ConfigError(
                "autoswitch.modelThresholds percentages must be between "
                f"{format_setting_value(lo)} and {format_setting_value(hi)}, "
                f"got '{piece}'"
            )
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        parsed.append((name, pct))
    if not parsed:
        raise ConfigError(
            "autoswitch.modelThresholds expects Name=PCT pairs (e.g. Fable=40)"
        )
    return format_model_thresholds(tuple(parsed))


def effective_model_names(settings: AutoSwitchSettings) -> tuple[str, ...]:
    """Models whose weekly window binds a decision.

    The union of ``autoswitch.model`` and every name in
    ``autoswitch.modelThresholds``, so ``Fable=40`` alone is enough to watch
    Fable — the shared ``model`` list is not a second switch that has to be
    set in lockstep.
    """
    names = list(parse_model_names(settings.model))
    seen = {name.lower() for name in names}
    for name, _pct in parse_model_thresholds(settings.model_thresholds):
        if name.lower() not in seen:
            names.append(name)
            seen.add(name.lower())
    return tuple(names)


@dataclass(frozen=True)
class GateThresholds:
    """Per-window switch thresholds. Missing labels inherit ``default``.

    ``overrides`` is keyed by lowercased label (``"5h"``, ``"7d"``, or a
    model display name). Entries equal to ``default`` are omitted, so "no
    overrides" means every gate shares one number and the engine's existing
    headroom math applies unchanged.
    """

    default: float
    overrides: dict[str, float]

    def for_label(self, label: str) -> float:
        return self.overrides.get(label.lower(), self.default)


def gate_thresholds(settings: AutoSwitchSettings) -> GateThresholds:
    """Effective per-gate thresholds for one settings snapshot.

    Each window takes the first one set: 5h ``threshold_5h``; 7d
    ``threshold_7d``, ``weekly_threshold``; a model window
    ``model_thresholds[M]``, ``weekly_threshold``; then ``threshold``.
    """
    default = float(settings.threshold)
    weekly = settings.weekly_threshold
    overrides: dict[str, float] = {}
    if weekly is not None:
        # Upstream's weeklyThreshold, as an alias under the specific keys.
        for label in ("7d", *parse_model_names(settings.model)):
            overrides[label.lower()] = float(weekly)
    if settings.threshold_5h is not None:
        overrides["5h"] = float(settings.threshold_5h)
    if settings.threshold_7d is not None:
        overrides["7d"] = float(settings.threshold_7d)
    for name, pct in parse_model_thresholds(settings.model_thresholds):
        overrides[name.lower()] = float(pct)
    overrides = {key: pct for key, pct in overrides.items() if pct != default}
    return GateThresholds(default, overrides)


def _clamped(settings: AutoSwitchSettings) -> AutoSwitchSettings:
    """Clamp values into the SETTING_SPECS ranges; bad types → the default."""

    def num(value, default: float | None, lo: float, hi: float) -> float | None:
        # json.loads accepts NaN/Infinity literals and min/max pass NaN
        # through unclamped, so a non-finite float is garbage too (a NaN line
        # would silently never be crossed). Floats only: an int is always
        # finite, and isfinite() would overflow on one too large for a float
        # — the clamp below handles that without ever converting it.
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or (isinstance(value, float) and not math.isfinite(value))
        ):
            return default
        return float(min(max(value, lo), hi))

    kwargs = {}
    for spec in SETTING_SPECS.values():
        if spec.section != "autoswitch":
            continue
        value = getattr(settings, spec.field)
        if spec.kind in ("float", "int"):
            clamped = num(value, spec.default, spec.lo, spec.hi)
            kwargs[spec.field] = int(clamped) if spec.kind == "int" else clamped
        elif spec.kind == "bool":
            kwargs[spec.field] = (
                value is True if spec.field == "warmup_five_hour" else bool(value)
            )
        elif spec.kind == "string":
            # A non-empty string keeps as-is; anything else reverts to default
            # (None) so a null/garbage settings.json value disables the filter.
            kwargs[spec.field] = value if isinstance(value, str) and value else spec.default
        elif spec.kind == "optional_float":
            # None inherits autoswitch.threshold. A bad type does too, rather
            # than crashing; an out-of-range number clamps like the other pcts.
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or (isinstance(value, float) and not math.isfinite(value))
            ):
                kwargs[spec.field] = None
            else:
                kwargs[spec.field] = float(min(max(value, spec.lo), spec.hi))
        elif spec.kind == "model_thresholds":
            pairs = parse_model_thresholds(value if isinstance(value, str) else None)
            kwargs[spec.field] = format_model_thresholds(pairs) if pairs else None
        else:  # choice
            if value not in spec.choices:
                _logger.warning(
                    "settings.json: unsupported %s %r; using %r",
                    spec.dotted, value, spec.default,
                )
                value = spec.default
            kwargs[spec.field] = value
    return AutoSwitchSettings(**kwargs)


def _read_raw(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        _logger.warning("Could not read %s (%s); using defaults", path, e)
        return {}
    if not isinstance(raw, dict):
        _logger.warning("%s is not a JSON object; using defaults", path)
        return {}
    return raw


def load_settings(backup_root: Path) -> AutoSwitchSettings:
    """Load the autoswitch section; missing/corrupt file or fields → defaults."""
    raw = _read_raw(settings_path(backup_root))
    section = raw.get("autoswitch")
    if not isinstance(section, dict):
        return AutoSwitchSettings()
    kwargs = {}
    for field, json_key in _AUTOSWITCH_KEYS.items():
        if json_key in section:
            kwargs[field] = section[json_key]
    try:
        settings = AutoSwitchSettings(**kwargs)
    except TypeError:
        settings = AutoSwitchSettings()
    return _clamped(settings)


def load_ui_settings(backup_root: Path) -> UiSettings:
    """Load the ui section; missing/corrupt file or bad fields → defaults."""
    raw = _read_raw(settings_path(backup_root))
    section = raw.get("ui")
    default = UiSettings()
    if not isinstance(section, dict):
        return default
    theme = section.get("theme", default.theme)
    if theme not in SETTING_SPECS["ui.theme"].choices:
        _logger.warning(
            "settings.json: unsupported ui.theme %r; using %r",
            theme, default.theme,
        )
        theme = default.theme
    view = section.get("view", default.view)
    if view not in SETTING_SPECS["ui.view"].choices:
        _logger.warning(
            "settings.json: unsupported ui.view %r; using %r",
            view, default.view,
        )
        view = default.view
    mask = section.get("mask", default.mask)
    if not isinstance(mask, bool):
        _logger.warning(
            "settings.json: unsupported ui.mask %r; using %r",
            mask, default.mask,
        )
        mask = default.mask
    star_prompt = section.get("starPrompt", default.star_prompt)
    if not isinstance(star_prompt, bool):
        _logger.warning(
            "settings.json: ui.starPrompt %r is not true or false; using %r",
            star_prompt, default.star_prompt,
        )
        star_prompt = default.star_prompt
    return UiSettings(theme=theme, view=view, mask=mask, star_prompt=star_prompt)


def load_credential_settings(backup_root: Path) -> CredentialSettings:
    """Load the credentials section; missing/corrupt/bad fields → defaults."""
    raw = _read_raw(settings_path(backup_root))
    section = raw.get("credentials")
    default = CredentialSettings()
    if not isinstance(section, dict):
        return default
    backend = section.get("backend", default.backend)
    if (
        not isinstance(backend, str)
        or backend not in SETTING_SPECS["credentials.backend"].choices
    ):
        _logger.warning(
            "settings.json: unsupported credentials.backend %r; using %r",
            backend, default.backend,
        )
        backend = default.backend
    return CredentialSettings(backend=backend)


def load_update_settings(backup_root: Path) -> UpdateSettings:
    """Load the updates section; missing/corrupt file or non-boolean → default."""
    raw = _read_raw(settings_path(backup_root))
    section = raw.get("updates")
    default = UpdateSettings()
    if not isinstance(section, dict):
        return default
    check = section.get("check", default.check)
    if not isinstance(check, bool):
        _logger.warning(
            "settings.json: updates.check %r is not true or false; using %r",
            check, default.check,
        )
        return default
    return UpdateSettings(check=check)


def load_run_settings(backup_root: Path) -> RunSettings:
    """Load the run section; missing/corrupt file or non-bool value → default."""
    section = _read_raw(settings_path(backup_root)).get("run")
    value = section.get("shareHistory") if isinstance(section, dict) else None
    if not isinstance(value, bool):
        return RunSettings()
    return RunSettings(share_history=value)


def save_settings(backup_root: Path, settings: AutoSwitchSettings) -> None:
    """Write the autoswitch section, preserving unknown keys and sections."""
    path = settings_path(backup_root)
    raw = _read_raw(path)
    raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
    section = raw.get("autoswitch")
    if not isinstance(section, dict):
        section = {}
    for field, json_key in _AUTOSWITCH_KEYS.items():
        section[json_key] = getattr(settings, field)
    raw["autoswitch"] = section
    atomic_write_json(path, raw)


def setting_spec(dotted_key: str) -> SettingSpec:
    """Look up a spec by dotted key; unknown keys raise with the valid list."""
    spec = SETTING_SPECS.get(dotted_key)
    if spec is None:
        raise ConfigError(
            f"unknown setting '{dotted_key}'\n"
            f"Valid keys: {', '.join(SETTING_SPECS)}"
        )
    return spec


_BOOL_WORDS = {
    "true": True, "1": True, "yes": True, "on": True,
    "false": False, "0": False, "no": False, "off": False,
}


def parse_setting_value(spec: SettingSpec, raw_value: str):
    """Strictly parse a CLI-provided string for `cswap config set`.

    Unlike the forgiving clamp on load, out-of-range or mistyped values raise
    ConfigError so the user learns about the problem when setting the value,
    not by silently degraded behavior at `cswap auto` time.
    """
    if spec.kind == "bool":
        # Never bool(str): bool("false") is True.
        parsed = _BOOL_WORDS.get(raw_value.strip().lower())
        if parsed is None:
            raise ConfigError(
                f"{spec.dotted} expects true or false (or 1/0, yes/no, on/off), "
                f"got '{raw_value}'"
            )
        return parsed
    if spec.kind == "choice":
        if raw_value not in spec.choices:
            raise ConfigError(
                f"{spec.dotted} must be one of: {', '.join(spec.choices)}"
            )
        return raw_value
    if spec.kind == "string":
        value = raw_value.strip()
        if not value:
            raise ConfigError(
                f"{spec.dotted} expects a non-empty value; use "
                f"'ccswap config unset {spec.dotted}' to clear it"
            )
        return value
    if spec.kind == "model_thresholds":
        return parse_model_thresholds_strict(raw_value)
    kind = "float" if spec.kind == "optional_float" else spec.kind
    try:
        value = int(raw_value) if kind == "int" else float(raw_value)
    except ValueError:
        noun = "an integer" if kind == "int" else "a number"
        # An optional number has no spelling for "unset" here; point at the
        # command that clears it instead of leaving "none" as a dead end.
        hint = (
            f"; use 'ccswap config unset {spec.dotted}' to clear it"
            if spec.default is None
            else ""
        )
        raise ConfigError(
            f"{spec.dotted} expects {noun}, got '{raw_value}'{hint}"
        ) from None
    if not spec.lo <= value <= spec.hi:
        raise ConfigError(
            f"{spec.dotted} must be between {format_setting_value(spec.lo)} "
            f"and {format_setting_value(spec.hi)}"
        )
    return value


def format_setting_value(value) -> str:
    """Render a settings value the way settings.json writes it."""
    if value is None:
        return "(none)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _read_raw_for_write(path: Path) -> dict:
    """Raw read for the config write path: a corrupt file errors, never {}.

    ``_read_raw``'s degrade-to-defaults is right for reads, but a
    read-modify-write starting from ``{}`` would replace a malformed (and
    maybe hand-recoverable) file with a near-empty one.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"could not read {path}: {e}") from e
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"{path} is not valid JSON ({e}); fix or delete it before "
            "changing settings"
        ) from e
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path} is not a JSON object; fix or delete it before "
            "changing settings"
        )
    return raw


def set_setting(backup_root: Path, dotted_key: str, raw_value: str):
    """Validate and persist one key for `cswap config set`; returns the value.

    Writes only the given key (plus schemaVersion) — deliberately not
    ``save_settings``, which writes every known key and would freeze the
    current defaults into the file, pinning users to them if a later version
    changes a default. Unknown keys and sections in the file survive.
    """
    spec = setting_spec(dotted_key)
    value = parse_setting_value(spec, raw_value)
    if dotted_key in ACTION_KEYS:
        from claude_swap.claude_integration import set_enabled

        set_enabled(backup_root, dotted_key, value)
        return value
    path = settings_path(backup_root)
    raw = _read_raw_for_write(path)
    raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
    section = raw.get(spec.section)
    if not isinstance(section, dict):
        section = {}
    section[spec.json_key] = value
    raw[spec.section] = section
    atomic_write_json(path, raw)
    return value


def unset_setting(backup_root: Path, dotted_key: str) -> bool:
    """Remove one key from settings.json; False if it wasn't set (no write)."""
    spec = setting_spec(dotted_key)
    path = settings_path(backup_root)
    raw = _read_raw_for_write(path)
    section = raw.get(spec.section)
    if not isinstance(section, dict) or spec.json_key not in section:
        return False
    raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
    del section[spec.json_key]
    if not section:
        del raw[spec.section]
    atomic_write_json(path, raw)
    return True


def effective_settings(
    backup_root: Path, only: str | None = None
) -> list[tuple[SettingSpec, object, bool]]:
    """(spec, effective value, explicitly set?) per key, in registry order.

    "Set" means the key is present in the raw file — an explicit value equal
    to the default still counts — so `cswap config`'s "(default)" marker
    reflects the file, not value equality. Action keys report Claude Code's
    live state instead, "set" when on. ``only`` limits the rows to one key,
    so a single lookup doesn't shell out to `claude plugin list` for nothing.
    """
    from claude_swap.claude_integration import is_enabled

    raw = _read_raw(settings_path(backup_root))
    loaded = {
        "autoswitch": load_settings(backup_root),
        "ui": load_ui_settings(backup_root),
        "credentials": load_credential_settings(backup_root),
        "run": load_run_settings(backup_root),
        "updates": load_update_settings(backup_root),
    }
    rows = []
    for spec in SETTING_SPECS.values():
        if only is not None and spec.dotted != only:
            continue
        if spec.dotted in ACTION_KEYS:
            value = is_enabled(spec.dotted)
            rows.append((spec, value, value))
            continue
        section = raw.get(spec.section)
        is_set = isinstance(section, dict) and spec.json_key in section
        rows.append((spec, getattr(loaded[spec.section], spec.field), is_set))
    return rows


def merged_with_cli(settings: AutoSwitchSettings, args) -> AutoSwitchSettings:
    """Overlay non-None CLI overrides (argparse Namespace) onto settings."""
    overrides = {}
    for attr, field in (
        ("threshold", "threshold"),
        ("weekly_threshold", "weekly_threshold"),
        ("interval", "interval_seconds"),
        ("cooldown", "cooldown_seconds"),
        ("include_api_key_accounts", "include_api_key_accounts"),
        ("model", "model"),
        ("strategy", "strategy"),
        ("threshold_5h", "threshold_5h"),
        ("threshold_7d", "threshold_7d"),
        ("model_thresholds", "model_thresholds"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            overrides[field] = value
    if not overrides:
        return settings
    return _clamped(dataclasses.replace(settings, **overrides))


def atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write JSON with the backup dir's 0600/0700 modes.

    Shared by settings.json and the autoswitch state file (and any future
    machine-local state files beside them).

    **Writes THROUGH a symlink, never over it.** A rename swaps a directory
    ENTRY and does not follow links, so renaming onto a symlinked path
    DETACHES the link: the write succeeds, the content is right, and the
    link target silently stops receiving updates — until something restores
    the link (a dotfiles deploy), taking every change written since with
    it. Same shape as #192/#193, which fixed ``session.py``'s own writer;
    this is the shared JSON writer. Three consequences, each deliberate:

    - A DANGLING link still writes where it points; linking a path is a
      request to write there.
    - The temp file is created beside the RESOLVED target, so the rename
      stays on one filesystem and remains atomic (beside the LINK it would
      hit EXDEV whenever the target lives on another mount).
    - The 0700 hardening stays on the directory cswap owns. Applying it to
      the resolved parent would narrow a directory belonging to something
      else, and raise ``PermissionError`` outright when that parent is not
      ours to chmod. The written file still gets 0600, and ``mkstemp``
      creates it 0600 to begin with, so the secret is never exposed.
    """
    target = publish_target(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        # `path.parent`, NOT the target's: see the docstring.
        os.chmod(path.parent, 0o700)
    atomic_write_text(target, json.dumps(data, indent=2))
