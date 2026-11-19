"""MenuBarController over fakes: no network, no Keychain, no real store."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from ccswap import json_output, menubar, menubar_controller as mc
from ccswap.exceptions import ClaudeSwitchError
from ccswap.menubar import EngineStoppedEvent, MenuBarSettings
from ccswap.models import AccountSnapshot, AccountsSnapshot
from ccswap.providers import PROVIDERS
from ccswap.usage_store import UsageEntry


def usage(five=10.0, weekly=20.0, key="seven_day"):
    return {
        "five_hour": {"pct": five, "resets_at": None},
        key: {"pct": weekly, "resets_at": None},
    }


def snap(*rows, active="1"):
    """rows: (number, email, usage dict | None, extra UsageEntry kwargs)."""
    accounts = tuple(
        AccountSnapshot(
            number=str(n), email=email, org_name="", org_uuid="", is_active=str(n) == active,
            kind="oauth", switchable=True,
            usage=UsageEntry(last_good=u, fetched_at=time.time() if u else None, age_s=1.0 if u else None),
        )
        for n, email, u in rows
    )
    return AccountsSnapshot(active_number=active, accounts=accounts, taken_at=time.time())


class FakeSwitcher:
    def __init__(self, snapshot=None, auth_file=None, config_path=None):
        self.snapshot = snapshot or snap()
        self.fetches: list = []
        self.calls: list = []
        self.raise_on_snapshot: Exception | None = None
        self.raise_on: Exception | None = None
        self.auth_file = auth_file
        self._config_path = config_path
        self.current = None  # codex: current_account_number; claude: (email,) tuple
        self._logger = SimpleNamespace(
            warning=lambda *a, **k: None, debug=lambda *a, **k: None, info=lambda *a, **k: None
        )

    def accounts_snapshot(self, fetch=None):
        self.fetches.append(fetch)
        if self.raise_on_snapshot:
            raise self.raise_on_snapshot
        return self.snapshot

    def _get_claude_config_path(self):
        return self._config_path

    def _get_current_account(self):
        return self.current

    def current_account_number(self):
        return self.current

    def _record(self, name, *a, **k):
        self.calls.append((name, a, k))
        if self.raise_on:
            raise self.raise_on

    def switch_to(self, identifier, json_output=False):
        self._record("switch_to", identifier)

    def switch(self, strategy=None, json_output=False):
        self._record("switch", strategy)

    def set_account_disabled(self, identifier, disabled):
        self._record("set_account_disabled", identifier, disabled)

    def remove_account(self, identifier, assume_yes=False):
        self._record("remove_account", identifier, assume_yes=assume_yes)

    def add_account(self, slot=None):
        self._record("add_account")

    def add_account_from_token(self, token, email, slot=None):
        self._record("add_account_from_token", email)


class FakeEngine:
    instances: list = []

    def __init__(self, switcher, settings, on_event, *, dry_run=False):
        self.switcher, self.on_event, self.stopped = switcher, on_event, False
        self.crash: Exception | None = None
        FakeEngine.instances.append(self)

    def run_loop(self):
        if self.crash:
            raise self.crash
        return 0

    def stop(self):
        self.stopped = True


class Silent(FakeEngine):
    """Engine whose loop returns at once; paired with a no-op or inline spawn."""

    def run_loop(self):
        return 0


@dataclass(frozen=True)
class Ev:
    kind: str
    text: str = "msg"
    message: str = "msg"
    dry_run: bool = False

    def human(self):
        return self.text


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A controller over three fake providers; threads run inline."""
    FakeEngine.instances = []
    switchers = {
        "claude": FakeSwitcher(snap((1, "a@x.com", usage()), (2, "b@x.com", usage(90, 95))),
                               config_path=tmp_path / "claude.json"),
        "codex": FakeSwitcher(snap((1, "c@x.com", usage(50, 60, "weekly"))), auth_file=tmp_path / "auth.json"),
        "agy": FakeSwitcher(snap((1, "g@x.com", None))),
    }
    providers = {pid: replace(PROVIDERS[pid], factory=lambda pid=pid: switchers[pid]) for pid in ("codex", "agy")}
    providers = {"claude": PROVIDERS["claude"], **providers}
    notes, alerts = [], []
    monkeypatch.setattr(mc, "_engine_class", lambda pid: Silent if pid in ("claude", "codex") else None)
    monkeypatch.setattr(mc, "_restart_note", lambda spec: f"restart {spec.id}")
    monkeypatch.setattr("ccswap.claude_integration.is_enabled", lambda key: key == "claude.mod")
    ctl = mc.MenuBarController(
        switchers["claude"], tmp_path, tmp_path / "claude-swap.log",
        notify=lambda *a: notes.append(a), alert=lambda *a: alerts.append(a), providers=providers,
    )
    ctl._spawn = lambda fn, *a: fn(*a)
    return SimpleNamespace(ctl=ctl, sw=switchers, notes=notes, alerts=alerts, root=tmp_path, providers=providers)


def refreshed(env):
    env.ctl.refresh()
    env.ctl.tick()
    return env.ctl


# ---- isolation ------------------------------------------------------------------

def test_factory_failure_shows_error_and_leaves_others(tmp_path):
    def boom():
        raise RuntimeError("no store")

    claude = FakeSwitcher(snap((1, "a@x.com", usage())), config_path=tmp_path / "c.json")
    providers = {"claude": PROVIDERS["claude"], "codex": replace(PROVIDERS["codex"], factory=boom)}
    ctl = mc.MenuBarController(claude, tmp_path, tmp_path / "l.log", notify=lambda *a: None,
                               alert=lambda *a: None, providers=providers)
    ctl._spawn = lambda fn, *a: fn(*a)
    ctl.refresh()
    ctl.tick()
    payload = ctl.payload()
    by = {p["id"]: p for p in payload["providers"]}
    assert by["codex"]["error"] == "no store" and by["codex"]["accounts"] == []
    assert by["claude"]["error"] is None and len(by["claude"]["accounts"]) == 1


def test_refresh_failure_keeps_others_and_last_snapshot(env):
    ctl = refreshed(env)
    env.sw["codex"].raise_on_snapshot = RuntimeError("down")
    ctl.refresh()
    ctl.tick()
    assert "down" in ctl.states["codex"].error
    assert ctl.states["codex"].adapted is not None  # last good kept
    assert ctl.states["claude"].error is None and ctl.states["agy"].error is None
    ctl.refresh()
    ctl.tick()
    assert ctl.states["codex"].error is not None
    env.sw["codex"].raise_on_snapshot = None
    ctl.refresh()
    ctl.tick()
    assert ctl.states["codex"].error is None


# ---- refresh rules ---------------------------------------------------------------

def test_store_only_while_engine_runs_per_provider(env):
    ctl = env.ctl
    ctl.set_auto("claude", True)
    ctl.refresh()
    assert env.sw["claude"].fetches == [set()]  # engine hosted: store-only
    assert env.sw["codex"].fetches == [None]  # no engine: normal pass
    ctl.refresh("claude", full=True)
    assert env.sw["claude"].fetches[-1] is None  # explicit refresh still passes


def test_one_inflight_refresh_per_provider(env):
    ctl = env.ctl
    spawned = []
    ctl._spawn = lambda fn, *a: spawned.append((fn, a))
    ctl.refresh()
    ctl.refresh()
    assert sorted(a[0] for _, a in spawned) == ["agy", "claude", "codex"]
    assert ctl.states["claude"].refreshing
    fn, a = spawned[0]
    fn(*a)  # worker finishes, guard released
    assert not ctl.states[a[0]].refreshing
    ctl.refresh(a[0])
    assert len(spawned) == 4


# ---- engines ---------------------------------------------------------------------

def test_codex_engine_crash_surfaces_as_engine_stopped(env):
    ctl = env.ctl
    ctl._spawn = lambda fn, *a: None
    ctl.set_auto("codex", True)
    engine = ctl.states["codex"].engine
    assert engine is not None
    ctl._on_engine_event("codex", EngineStoppedEvent(engine, "RuntimeError: boom"))
    assert ctl.tick() is True
    state = ctl.states["codex"]
    assert state.engine is None and state.engine_failure == "RuntimeError: boom"
    assert any("Codex auto-switch stopped" in n[1] for n in env.notes)
    entry = {p["id"]: p for p in ctl.payload()["providers"]}["codex"]
    assert entry["autoSwitch"] == {"enabled": True, "running": False, "failure": "RuntimeError: boom"}


def test_crashing_run_loop_posts_engine_stopped(env, monkeypatch):
    class Crash(FakeEngine):
        def run_loop(self):
            raise ValueError("bad")

    monkeypatch.setattr(mc, "_engine_class", lambda pid: Crash)
    ctl = env.ctl
    ctl.set_auto("codex", True)  # inline spawn: the loop crashed and queued the event
    ctl.tick()
    assert ctl.states["codex"].engine_failure == "ValueError: bad"


def test_stale_predecessor_exit_is_ignored(env):
    ctl = env.ctl
    ctl._spawn = lambda fn, *a: None
    ctl.set_auto("codex", True)
    old = ctl.states["codex"].engine
    ctl.restart_engine("codex")
    new = ctl.states["codex"].engine
    assert new is not old and old.stopped
    ctl._on_engine_event("codex", EngineStoppedEvent(old, "late"))
    ctl.tick()
    assert ctl.states["codex"].engine is new and ctl.states["codex"].engine_failure is None
    assert not env.notes


def test_antigravity_has_no_engine_or_toggle(env):
    ctl = env.ctl
    assert ctl.set_auto("agy", True) is False
    entry = {p["id"]: p for p in ctl.payload()["providers"]}["agy"]
    assert entry["autoSwitch"] is None
    assert "agy" not in ctl._watch


def test_engine_start_failure_sets_failure_and_notifies(env, monkeypatch):
    def broken(pid):
        raise RuntimeError("cannot start")

    monkeypatch.setattr(mc, "_engine_class", broken)
    env.ctl.set_auto("claude", True)
    assert env.ctl.states["claude"].engine_failure == "cannot start"
    assert any("failed to start" in n[1] for n in env.notes)


def test_start_and_stop_follow_settings(env):
    env.ctl.settings.codex_auto_switch_enabled = True
    env.ctl._spawn = lambda fn, *a: None
    env.ctl.start()
    assert env.ctl.states["codex"].engine and not env.ctl.states["claude"].engine
    engine = env.ctl.states["codex"].engine
    env.ctl.stop()
    assert engine.stopped and env.ctl.states["codex"].engine is None


# ---- events -> notifications -------------------------------------------------------

def test_event_notification_mapping(env):
    ctl = env.ctl
    ctl._on_engine_event("claude", Ev("switch", "1 to 2"))
    ctl._on_engine_event("codex", Ev("switch", "1 to 2"))
    ctl._on_engine_event("codex", Ev("all-exhausted"))
    ctl._on_engine_event("claude", Ev("all-exhausted"))
    ctl._on_engine_event("claude", Ev("waste-warning"))
    ctl._on_engine_event("claude", Ev("switch", dry_run=True))
    ctl._on_engine_event("claude", Ev("poll"))  # ignored kind
    assert ctl.tick() is True
    assert [n[1] for n in env.notes] == [
        "Auto-switched account", "Codex auto-switched account",
        "All Codex accounts exhausted", "All accounts exhausted", "Quota about to go unused",
    ]
    assert all(n[0] == "ccswap" for n in env.notes)


def test_engine_error_repeats_are_suppressed_per_provider(env):
    ctl = env.ctl
    for _ in range(3):
        ctl._on_engine_event("claude", Ev("error", "tick 5 failed", "tick 5 failed"))
    ctl._on_engine_event("codex", Ev("error", "tick 5 failed", "tick 5 failed"))
    ctl.tick()
    titles = [n[1] for n in env.notes]
    assert titles == ["Auto-switch error", "Codex auto-switch error"]


# ---- actions -----------------------------------------------------------------------

def test_restart_note_per_provider_on_switch(env):
    ctl = env.ctl
    assert ctl.switch_to("claude", 2) and ctl.switch_to("codex", 1) and ctl.switch_to("agy", 1)
    assert env.notes == [
        ("ccswap", "Account switched", "restart claude"),
        ("ccswap", "Codex account switched", "restart codex"),
        ("ccswap", "Antigravity account switched", "restart agy"),
    ]
    assert env.sw["codex"].calls == [("switch_to", ("1",), {})]


def test_real_restart_notes_name_the_provider():
    claude = mc._restart_note(PROVIDERS["claude"])
    agy = mc._restart_note(PROVIDERS["agy"])
    assert "restart Claude Code" in claude and "Antigravity" in agy


def test_switch_error_alerts_and_returns_false(env):
    env.sw["claude"].raise_on = ClaudeSwitchError("locked")
    assert env.ctl.switch_to("claude", 2) is False
    assert env.alerts == [("ccswap", "locked")] and not env.notes


def test_capability_gates(env):
    ctl = env.ctl
    assert ctl.rotate("agy", None) is False  # usage-only provider
    assert ctl.rotate("claude", "bogus") is False
    assert ctl.set_disabled("agy", 1, True) is False
    assert ctl.rotate("codex", "best") is True
    assert ctl.set_disabled("codex", 1, True) is True
    assert ctl.switch_to("nope", 1) is False
    assert env.sw["agy"].calls == []
    assert env.sw["claude"].calls == []
    assert env.sw["codex"].calls[0] == ("switch", ("best",), {})


def test_remove_add_token_and_refresh_creds(env):
    ctl = env.ctl
    assert ctl.remove("codex", 1)
    assert env.sw["codex"].calls == [("remove_account", ("1",), {"assume_yes": True})]
    assert ctl.add_token("a@x.com", " tok ") and env.sw["claude"].calls[-1][0] == "add_account_from_token"
    assert ctl.refresh_creds() is False  # no active login
    assert "No active Claude Code login" in env.alerts[-1][1]
    env.sw["claude"].current = ("a@x.com", "uuid")
    assert ctl.refresh_creds() is True


def test_threshold_restarts_running_engines(env):
    ctl = env.ctl
    ctl._spawn = lambda fn, *a: None
    ctl.set_auto("claude", True)
    old = ctl.states["claude"].engine
    assert ctl.set_threshold(95) is True
    assert old.stopped and ctl.states["claude"].engine is not old
    assert ctl.threshold() == 95


# ---- watchers -----------------------------------------------------------------------

def test_watchers_refresh_only_their_own_provider(env):
    ctl = refreshed(env)
    for sw in env.sw.values():
        sw.fetches.clear()
    env.root.joinpath("auth.json").write_text("{}")
    env.sw["codex"].current = "2"  # differs from the snapshot's active "1"
    assert ctl.tick() is False  # results are applied on the next tick
    assert len(env.sw["codex"].fetches) == 1
    assert env.sw["claude"].fetches == [] and env.sw["agy"].fetches == []

    env.root.joinpath("claude.json").write_text("{}")
    env.sw["claude"].current = ("zzz@x.com", "u")
    ctl.tick()
    assert len(env.sw["claude"].fetches) == 1
    assert len(env.sw["codex"].fetches) == 1 and env.sw["agy"].fetches == []


def test_watcher_ignores_unchanged_identity(env):
    ctl = refreshed(env)
    env.sw["codex"].fetches.clear()
    env.root.joinpath("auth.json").write_text("{}")
    env.sw["codex"].current = "1"  # same active account: an unrelated token refresh
    ctl.tick()
    assert env.sw["codex"].fetches == []


# ---- payload -------------------------------------------------------------------------

def test_payload_keys_and_provider_entries(env):
    ctl = refreshed(env)
    p = ctl.payload()
    assert set(p) == {"schemaVersion", "lastCheckedAt", "health", "title", "settings", "history", "providers"}
    assert p["schemaVersion"] == 1 and isinstance(p["lastCheckedAt"], float)
    assert set(p["health"]) == {"label", "attention"}
    assert set(p["title"]) == {"setting", "resolved", "text"}
    assert set(p["settings"]) == {
        "style", "titleProvider", "titlePct", "showAccountName", "titleScoped",
        "refreshInterval", "threshold", "strategy", "claudeIntegrations",
    }
    assert p["settings"]["claudeIntegrations"] == {"statusline": False, "mod": False}
    assert [e["id"] for e in p["providers"]] == ["claude", "codex", "agy"]
    for entry in p["providers"]:
        base = json_output.provider_entry(env.providers[entry["id"]], None, None)
        for key in base:
            assert key in entry
        assert entry["capabilities"] == base["capabilities"]
        assert {"autoSwitch", "refreshing", "addToken"} <= set(entry)
    claude = p["providers"][0]
    assert claude["activeAccountNumber"] == 1 and claude["addToken"] is True
    assert claude["autoSwitch"] == {"enabled": False, "running": False, "failure": None}
    row = claude["accounts"][0]
    assert {"number", "email", "alias", "active", "disabled", "windows", "windowsSource",
            "usageStatus", "usage", "note", "loginNote"} <= set(row)
    assert row["number"] == 1 and row["active"] is True
    assert [w["id"] for w in row["windows"]] == ["5h", "7d"]
    assert p["providers"][1]["addToken"] is False


def test_claude_integrations_load_in_payload(env):
    ctl = env.ctl
    ctl._start_claude_work()
    ctl.tick()
    assert ctl.payload()["settings"]["claudeIntegrations"] == {"statusline": False, "mod": True}


def test_antigravity_windows_carry_scope(env):
    agy_usage = {"scoped": [{"name": "Gemini Pro", "pct": 40.0, "window": "5h", "resets_at": None,
                             "group": "gemini", "bucket_id": "b1", "bucket_name": "Pro"}]}
    env.sw["agy"].snapshot = snap((1, "g@x.com", agy_usage))
    ctl = refreshed(env)
    windows = {p["id"]: p for p in ctl.payload()["providers"]}["agy"]["accounts"][0]["windows"]
    assert windows and windows[0]["scope"] == {"group": "gemini", "bucket": "Pro"}
    assert windows[0]["durationS"] == 5 * 3600


def test_sentinel_note_is_trimmed_for_a_row(env):
    entry = UsageEntry(sentinel="token expired", last_good=None)
    acc = AccountSnapshot("1", "a@x.com", "", "", True, "oauth", True, entry, login_expires_at=None)
    env.sw["claude"].snapshot = AccountsSnapshot("1", (acc,), time.time())
    ctl = refreshed(env)
    row = ctl.payload()["providers"][0]["accounts"][0]
    assert row["note"] and len(row["note"]) <= menubar.MENU_NOTE_LIMIT


# ---- title / display ---------------------------------------------------------------------

def test_title_provider_follows_setting_including_auto(env):
    ctl = refreshed(env)
    assert ctl.title_provider() == "claude"
    ctl.settings.title_provider = "codex"
    assert ctl.title_provider() == "codex"
    ctl.settings.title_provider = "auto"
    # claude's active account (1) reads 20%, codex's 60%: auto picks codex; the title carries
    # no provider tag, the tooltip names it instead
    assert ctl.title_provider() == "codex"
    assert ctl.title() == f"{menubar.ICON} c · 50% · 60%"
    assert ctl.title_label() == "Codex"
    assert ctl.payload()["title"]["resolved"] == "codex"
    ctl.settings.title_provider = "claude"
    assert ctl.title_label() == "Claude Code"


def test_set_display_rejects_bogus_and_persists(env):
    ctl = env.ctl
    changed = []
    ctl.on_interval_changed = changed.append
    assert ctl.set_display("title_provider", "bogus") is False
    assert ctl.set_display("title_pct", "never") is False
    assert ctl.set_display("show_account_name", "yes") is False
    assert ctl.set_display("refresh_interval", 5) is False
    assert ctl.set_display("refresh_interval", True) is False
    assert ctl.set_display("style", "tiny") is False
    assert ctl.set_display("nonsense", 1) is False
    assert ctl.settings.title_provider == "claude"

    assert ctl.set_display("title_provider", "auto")
    assert ctl.set_display("style", "classic")
    assert ctl.set_display("title_pct", "5h")
    assert ctl.set_display("show_account_name", False)
    assert ctl.set_display("title_scoped", True)
    assert ctl.set_display("refresh_interval", 300)
    assert changed == [300]
    loaded = MenuBarSettings.load(env.root)
    assert (loaded.title_provider, loaded.style, loaded.title_pct) == ("auto", "classic", "5h")
    assert (loaded.show_account_name, loaded.title_scoped, loaded.refresh_interval) == (False, True, 300)
    assert (env.root / "settings.json").exists()


def test_history_reads_switch_lines(env):
    env.ctl.log_path.write_text("2026-06-27 02:06:11 - INFO - Switched from account 3 to 1\n")
    assert env.ctl.history() == ["3 → 1   2026-06-27 02:06"]


def test_health_change_requests_repaint(env):
    ctl = env.ctl
    assert ctl.tick() is True  # first reading of the health row
    assert ctl.tick() is False


def test_failed_save_leaves_memory_and_engines_untouched(env):
    # A corrupt settings.json makes every save raise; the toggle must not half-apply.
    (env.root / "settings.json").write_text("{ broken", encoding="utf-8")
    ctl = env.ctl
    assert ctl.set_auto("claude", True) is False
    assert ctl.settings.auto_switch_enabled is False
    assert ctl.states["claude"].engine is None
    assert ctl.set_display("title_provider", "auto") is False
    assert ctl.settings.title_provider == "claude"
    assert len(env.alerts) == 2 and "Couldn't save" in env.alerts[0][1]


def test_unknown_integration_state_is_not_toggled(env):
    ctl = env.ctl
    ctl._claude_state["claude.statusline"] = None
    assert ctl.toggle_claude_integration("statusline") is False


def test_stop_with_wait_joins_engine_threads(env):
    import threading

    ctl = env.ctl
    release = threading.Event()
    finished = []

    def slow():
        release.wait(5)
        finished.append(True)

    worker = threading.Thread(target=slow, daemon=True)
    worker.start()
    ctl._engine_threads["claude"] = worker
    threading.Timer(0.2, release.set).start()
    ctl.stop(wait=5)
    assert finished == [True]  # stop() waited for the engine thread


def test_payload_carries_plan_and_strategy(env):
    from types import SimpleNamespace as NS

    assert mc._plan(NS(subscription_plan={"plan": {"tier": "default_claude_max_5x"}}, org_name="Acme"), "Claude Code")
    assert mc._plan(NS(subscription_plan=None, org_name="Codex Plus"), "Codex") == "Plus"
    assert mc._plan(NS(subscription_plan=None, org_name="Acme"), "Codex") is None
    p = refreshed(env).payload()
    assert p["settings"]["strategy"] in ("best", "consume-first")
    assert all("plan" in a for prov in p["providers"] for a in prov["accounts"])


def test_add_login_says_what_happened(env):
    ctl = refreshed(env)
    sw = env.sw["codex"]
    # already managed: the store doesn't change, so the notification says which account it is
    sw.add_account = lambda: None
    assert ctl.add_login("codex") is True
    assert any("already account 1" in n[1] for n in env.notes)
    # a new account appears in the store after add_account runs
    def add():
        sw.snapshot = snap((1, "c@x.com", usage(50, 60, "weekly")), (2, "new@x.com", None))
    sw.add_account = add
    assert ctl.add_login("codex") is True
    assert any("account 2 added" in n[1] and n[2] == "new@x.com" for n in env.notes)


def test_action_thread_alerts_and_refresh_reach_main_via_tick(env):
    import threading

    shown = []
    env.sw["claude"].raise_on = ClaudeSwitchError("locked")
    t = threading.Thread(target=env.ctl.switch_to, args=("claude", 2))
    t.start()
    t.join(5)
    assert env.alerts == []  # queued, not shown off the main thread
    env.ctl.tick()
    assert env.alerts == [("ccswap", "locked")]

    env.sw["claude"].raise_on = None
    env.ctl._spawn = lambda fn, *a: shown.append(threading.current_thread()) or fn(*a)
    t = threading.Thread(target=env.ctl.switch_to, args=("claude", 2))
    t.start()
    t.join(5)
    assert env.notes == [] and shown == []  # notify and the follow-up refresh wait for tick
    env.ctl.tick()
    assert env.notes and shown == [threading.main_thread()]


def test_a_popover_toggle_keeps_keys_changed_from_the_cli(env):
    from ccswap import settings

    ctl = env.ctl
    settings.set_setting(ctl.backup_root, "menubar.titlePct", "5h")  # `ccswap config set` while running
    assert ctl.set_display("show_account_name", not ctl.settings.show_account_name)
    assert menubar.MenuBarSettings.load(ctl.backup_root).title_pct == "5h"


def test_join_workers_waits_for_a_running_refresh(env):
    import threading

    ctl = env.ctl
    gate, done = threading.Event(), []
    ctl._spawn = mc._spawn  # real threads
    ctl._track(mc._spawn(lambda: (gate.wait(5), done.append(1))))
    threading.Timer(0.1, gate.set).start()
    ctl.join_workers(wait=5)
    assert done == [1]
