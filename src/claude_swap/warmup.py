"""Opt-in keeper for Claude subscription five-hour usage windows.

The usage endpoint is checked first. A real, minimal Haiku request is sent only
when a fresh, evidence-bearing snapshot says the five-hour window is absent (or
its advertised reset is already past). Hollow, unknown, failed, disabled,
non-OAuth, and weekly-exhausted accounts fail closed.

Warm requests run through persistent per-account session profiles, never by
switching the globally active login. A small state file prevents duplicate
spend when a just-warmed usage snapshot remains briefly cached or the process
is restarted.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from claude_swap import oauth
from claude_swap.exceptions import ClaudeSwitchError, PromptOutcomeUnknown, WarmupError
from claude_swap.locking import FileLock
from claude_swap.models import AccountSnapshot
from claude_swap.poll_policy import parse_reset_ts
from claude_swap.session import SessionManager
from claude_swap.settings import atomic_write_json
from claude_swap.usage_store import STALE_OK_S

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher


DEFAULT_INTERVAL_SECONDS = 600.0
MIN_INTERVAL_SECONDS = 300.0
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_MODEL = "claude-haiku-4-5"
FIVE_HOUR_SECONDS = 5 * 60 * 60
PENDING_GUARD_SECONDS = 10 * 60
STATE_SCHEMA_VERSION = 1
WARMUP_PROMPT = "Reply only: OK"


def _usage_has_real_detail(usage: dict) -> bool:
    """Whether a usage snapshot contains evidence beyond a hollow zero row."""
    for _, pct, resets_at in oauth.relevant_windows(usage, models=("all",)):
        if resets_at or pct > 0:
            return True
    return isinstance(usage.get("spend"), dict)


@dataclass(frozen=True)
class WarmupEvent:
    """One account-level decision made during a warm-up tick."""

    kind: str
    account_number: str
    email: str
    detail: str


@dataclass(frozen=True)
class WarmupSummary:
    """Aggregate result of one warm-up tick."""

    warmed: int = 0
    would_warm: int = 0
    skipped: int = 0
    failed: int = 0


class WarmupEngine:
    """Inspect all managed accounts and warm only confirmed-cold windows."""

    def __init__(
        self,
        switcher: ClaudeAccountSwitcher,
        *,
        emit: Callable[[WarmupEvent], None],
        session_manager: SessionManager | None = None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        model: str = DEFAULT_MODEL,
        dry_run: bool = False,
        clock: Callable[[], float] = time.time,
    ):
        self.switcher = switcher
        self.emit = emit
        self.sessions = session_manager or SessionManager(switcher)
        self.interval_seconds = interval_seconds
        self.timeout_seconds = timeout_seconds
        self.model = model
        self.dry_run = dry_run
        self.clock = clock
        self.state_path = switcher.backup_dir / "warmup_state.json"
        self.lock_path = switcher.backup_dir / ".warmup.lock"
        self._stopped = threading.Event()

    def stop(self) -> None:
        """Request a foreground loop shutdown."""
        self._stopped.set()

    def run_loop(self) -> int:
        """Run ticks until stopped; return nonzero if the final tick failed."""
        exit_code = 0
        while not self._stopped.is_set():
            summary = self.tick()
            exit_code = 1 if summary.failed else 0
            if self._stopped.wait(self.interval_seconds):
                break
        return exit_code

    def tick(self) -> WarmupSummary:
        """Run one serialized usage-check and warm pass."""
        with FileLock(self.lock_path, timeout=1.0):
            return self._tick_locked()

    def _tick_locked(self) -> WarmupSummary:
        now = self.clock()
        state = self._load_state()
        snapshot = self.switcher.accounts_snapshot(fetch=None)
        freshen = {
            account.number
            for account in snapshot.accounts
            if self._needs_fresh_probe(account, state, now)
        }
        if freshen:
            # An explicit fetch set may bypass an idle account's long poll plan,
            # while UsageStore still enforces its serve TTL, claims, and backoff.
            # Only stale accounts that look cold are escalated, and a persisted
            # successful warm suppresses further probes for the whole window.
            snapshot = self.switcher.accounts_snapshot(fetch=freshen)
        warmed = would_warm = skipped = failed = 0

        for account in snapshot.accounts:
            reason = self._skip_reason(account, state, now)
            if reason is not None:
                skipped += 1
                self._emit(account, reason, self._reason_detail(reason))
                continue

            if self.dry_run:
                would_warm += 1
                self._emit(
                    account,
                    "would-warm",
                    f"would send one minimal {self.model} request",
                )
                continue

            self._mark_pending(state, account, now)
            self._save_state(state)
            try:
                result = self.sessions.run_prompt(
                    account.number,
                    self._claude_args(),
                    timeout=self.timeout_seconds,
                    expected_identity=(account.email, account.org_uuid),
                )
            except PromptOutcomeUnknown as exc:
                # Anthropic may have accepted the request before the local
                # timeout. Keep pendingAt so a stale cold snapshot cannot cause
                # an immediate duplicate; its expiry forces a fresh usage probe.
                failed += 1
                self._emit(account, "failed", self._clean_detail(str(exc)))
                continue
            except (ClaudeSwitchError, OSError) as exc:
                self._clear_pending(state, account)
                self._save_state(state)
                failed += 1
                self._emit(account, "failed", self._clean_detail(str(exc)))
                continue

            if result.returncode != 0:
                # A child can exit nonzero after the service accepted its
                # message. Preserve pendingAt and require a later fresh probe.
                failed += 1
                self._emit(
                    account,
                    "failed",
                    f"Claude exited with code {result.returncode}; retry protected",
                )
                continue

            self._mark_warmed(state, account, now)
            self._save_state(state)
            warmed += 1
            self._emit(
                account,
                "warmed",
                f"started a five-hour window with one minimal {self.model} request",
            )

        return WarmupSummary(
            warmed=warmed,
            would_warm=would_warm,
            skipped=skipped,
            failed=failed,
        )

    def _needs_fresh_probe(
        self, account: AccountSnapshot, state: dict, now: float
    ) -> bool:
        if (
            account.disabled
            or account.kind != "oauth"
            or not account.switchable
            or self._state_is_recent(state, account, now)
        ):
            return False
        entry = account.usage
        if entry.last_error is None and entry.age_s is not None and entry.age_s <= STALE_OK_S:
            return False
        usage = entry.last_good
        if not isinstance(usage, dict):
            return True

        weekly = usage.get("seven_day")
        if isinstance(weekly, dict) and isinstance(weekly.get("pct"), (int, float)):
            if float(weekly["pct"]) >= 100.0:
                reset = parse_reset_ts(weekly.get("resets_at"))
                return reset is not None and reset <= now

        five_hour = usage.get("five_hour")
        if not isinstance(five_hour, dict):
            return True
        resets_at = five_hour.get("resets_at")
        if not resets_at:
            return five_hour.get("pct") == 0
        reset = parse_reset_ts(resets_at)
        return reset is not None and reset <= now

    def _skip_reason(self, account: AccountSnapshot, state: dict, now: float) -> str | None:
        if account.disabled:
            return "disabled"
        if account.kind != "oauth":
            return "not-oauth"
        if not account.switchable:
            return "unavailable"
        if self._state_is_recent(state, account, now):
            return "recently-warmed"

        entry = account.usage
        usage = entry.decision_value()
        if (
            not isinstance(usage, dict)
            or entry.last_error is not None
            or entry.age_s is None
            or entry.age_s > STALE_OK_S
        ):
            return "usage-unavailable"

        weekly = usage.get("seven_day")
        if not isinstance(weekly, dict) or not isinstance(
            weekly.get("pct"), (int, float)
        ):
            return "usage-unavailable"
        if float(weekly["pct"]) >= 100.0 or self._model_weekly_exhausted(usage):
            return "weekly-exhausted"

        # The provider can return an all-zero/no-reset placeholder for an
        # inactive credential. It is indistinguishable from a cold window by
        # five-hour fields alone, so require evidence elsewhere in the payload
        # and fail closed rather than spend quota on an uncertain account.
        if not _usage_has_real_detail(usage):
            return "usage-unavailable"

        if "five_hour" not in usage:
            return None
        five_hour = usage.get("five_hour")
        if not isinstance(five_hour, dict) or not isinstance(
            five_hour.get("pct"), (int, float)
        ):
            return "usage-unavailable"
        resets_at = five_hour.get("resets_at")
        if not resets_at:
            # The live endpoint's cold shape is {pct: 0} with no reset.
            # A non-zero window missing its deadline is ambiguous, so skip it.
            return None if float(five_hour["pct"]) == 0.0 else "usage-unavailable"
        reset_ts = parse_reset_ts(resets_at)
        if reset_ts is None:
            return "usage-unavailable"
        if reset_ts > now:
            return "live"
        return None

    def _model_weekly_exhausted(self, usage: dict) -> bool:
        scoped = usage.get("scoped")
        if not isinstance(scoped, list):
            return False
        wanted = self.model.casefold()
        family = next(
            (
                name
                for name in ("haiku", "sonnet", "opus", "fable")
                if name in wanted
            ),
            wanted,
        )
        for window in scoped:
            if not isinstance(window, dict):
                continue
            name = window.get("name")
            pct = window.get("pct")
            if (
                isinstance(name, str)
                and family in name.casefold()
                and isinstance(pct, (int, float))
                and float(pct) >= 100.0
            ):
                return True
        return False

    def _claude_args(self) -> list[str]:
        return [
            "--print",
            "--model",
            self.model,
            "--effort",
            "low",
            "--safe-mode",
            "--tools",
            "",
            "--no-session-persistence",
            WARMUP_PROMPT,
        ]

    def _load_state(self) -> dict:
        if not self.state_path.exists():
            return {"schemaVersion": STATE_SCHEMA_VERSION, "accounts": {}}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WarmupError(
                f"Could not read {self.state_path}; refusing to risk duplicate "
                f"warm-up requests: {exc}"
            ) from exc
        if (
            not isinstance(data, dict)
            or data.get("schemaVersion") != STATE_SCHEMA_VERSION
            or not isinstance(data.get("accounts"), dict)
        ):
            raise WarmupError(
                f"Invalid warm-up state in {self.state_path}; refusing to risk "
                "duplicate requests."
            )
        return data

    def _save_state(self, state: dict) -> None:
        try:
            atomic_write_json(self.state_path, state)
        except (OSError, ValueError) as exc:
            raise WarmupError(
                f"Could not persist warm-up state to {self.state_path}: {exc}"
            ) from exc

    @staticmethod
    def _state_row(state: dict, account: AccountSnapshot) -> dict | None:
        row = state["accounts"].get(account.number)
        if not isinstance(row, dict):
            return None
        if row.get("email") != account.email or row.get("orgUuid", "") != account.org_uuid:
            return None
        return row

    def _state_is_recent(self, state: dict, account: AccountSnapshot, now: float) -> bool:
        row = self._state_row(state, account)
        if row is None:
            return False
        last_warm = row.get("lastWarmAt")
        if isinstance(last_warm, (int, float)) and now < float(last_warm) + FIVE_HOUR_SECONDS:
            return True
        pending = row.get("pendingAt")
        return isinstance(pending, (int, float)) and now < float(pending) + PENDING_GUARD_SECONDS

    @staticmethod
    def _row_for(account: AccountSnapshot) -> dict:
        return {"email": account.email, "orgUuid": account.org_uuid}

    def _mark_pending(self, state: dict, account: AccountSnapshot, now: float) -> None:
        row = self._state_row(state, account) or self._row_for(account)
        row["pendingAt"] = now
        state["accounts"][account.number] = row

    def _clear_pending(self, state: dict, account: AccountSnapshot) -> None:
        row = self._state_row(state, account)
        if row is not None:
            row.pop("pendingAt", None)

    def _mark_warmed(self, state: dict, account: AccountSnapshot, now: float) -> None:
        row = self._state_row(state, account) or self._row_for(account)
        row.pop("pendingAt", None)
        row["lastWarmAt"] = now
        state["accounts"][account.number] = row

    @staticmethod
    def _clean_detail(detail: str) -> str:
        lines = [line.strip() for line in detail.splitlines() if line.strip()]
        clean = lines[-1] if lines else "unknown error"
        return clean if len(clean) <= 240 else clean[:237] + "..."

    @staticmethod
    def _reason_detail(reason: str) -> str:
        return {
            "disabled": "disabled accounts are not warmed",
            "not-oauth": "API-key accounts are not warmed",
            "unavailable": "account is not currently switchable",
            "recently-warmed": "a successful or in-progress warm is still protected",
            "usage-unavailable": "fresh successful usage data is unavailable; skipped safely",
            "weekly-exhausted": "weekly quota is exhausted; no request sent",
            "live": "five-hour window is already active",
        }[reason]

    def _emit(self, account: AccountSnapshot, kind: str, detail: str) -> None:
        self.emit(WarmupEvent(kind, account.number, account.email, detail))
