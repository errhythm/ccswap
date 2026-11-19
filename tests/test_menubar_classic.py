"""menu_spec is pure: tests drive it with a fake controller, no rumps."""

import subprocess
import sys
from types import SimpleNamespace

from ccswap import menubar_classic as mc
from ccswap.providers import PROVIDERS, Capabilities, ProviderSpec


def _acc(num, email, active=False, disabled=False):
    return (num, email, active, {"five_hour": {"pct": 10}}, None, None, disabled, None, None, False)


def _state(*accs):
    return SimpleNamespace(adapted={"accounts": list(accs)}, error=None)


class Fake:
    def __init__(self):
        self.settings = SimpleNamespace(
            title_provider="codex", title_pct="both", show_account_name=True,
            title_scoped=False, refresh_interval=60,
        )
        self.states = {
            "claude": _state(_acc(1, "a@x.com", True)),
            "codex": _state(_acc(1, "c@x.com", True), _acc(2, "d@x.com", disabled=True)),
            "agy": _state(_acc(1, "g@x.com", True)),
        }

    def payload(self):
        return {
            "settings": {"threshold": 95, "claudeIntegrations": {"statusline": True, "mod": False}},
            "providers": [
                {"id": "claude", "autoSwitch": {"enabled": True, "running": True}},
                {"id": "codex", "autoSwitch": {"enabled": False, "running": False}},
                {"id": "agy", "autoSwitch": None},
            ],
        }

    def health(self):
        return ("OK", False)

    def history(self):
        return []


def _walk(rows):
    for r in rows:
        yield r
        yield from _walk(r.children)


def _find(rows, label):
    return [r for r in _walk(rows) if r.label == label]


def test_one_section_per_provider_in_order():
    rows = mc.menu_spec(Fake())
    labels = [s.label for s in PROVIDERS.values()]
    headers = [r.label.split("  ·")[0] for r in rows if r.action is None and not r.children
               and r.label.split("  ·")[0] in labels]
    assert headers == ["Claude Code", "Codex", "Antigravity"]
    sw = [r for r in rows if r.action == "switch_to"]
    assert [(r.args, r.state) for r in sw] == [
        (("claude", 1), True), (("codex", 1), True), (("codex", 2), False), (("agy", 1), True)]


def test_strategy_rows_and_disable_rows():
    rows = mc.menu_spec(Fake())
    rot = [r.args for r in rows if r.action == "rotate"]
    assert ("codex", "best") in rot and ("codex", None) in rot
    assert not [a for a in rot if a[0] == "agy"]
    manage = {r.label: r for r in rows if r.label.startswith("Manage ")}
    assert _find([manage["Manage Antigravity"]], "Disable / enable account") == []
    assert _find([manage["Manage Codex"]], "Disable / enable account")
    assert _find([manage["Manage Claude Code"]], "From setup-token…")
    assert not _find([manage["Manage Codex"]], "From setup-token…")


def test_usage_only_provider_has_no_switch_action(monkeypatch):
    spec = ProviderSpec("ro", "ReadOnly", Capabilities(switch=False), lambda **k: None)
    monkeypatch.setattr(mc, "PROVIDERS", {"ro": spec})
    f = Fake()
    f.states = {"ro": _state(_acc(1, "r@x.com", True))}
    rows = mc.menu_spec(f)
    assert not [r for r in rows if r.action == "switch_to"]
    assert any("r@x.com" in r.label for r in rows)


def test_title_shows_submenu_and_auto_toggles():
    rows = mc.menu_spec(Fake())
    (title,) = _find(rows, "Title shows")
    assert [c.label for c in title.children] == ["Auto", "Claude Code", "Codex", "Antigravity"]
    assert [c.args[1] for c in title.children if c.state] == ["codex"]
    toggles = [r for r in _walk(rows) if r.action == "set_auto"]
    assert [(r.args, r.state) for r in toggles] == [(("claude", False), True), (("codex", True), False)]
    assert _find(rows, "Use popover")[0].args == ("style", "popover")


def test_imports_without_rumps():
    code = "import sys, ccswap.menubar_classic; sys.exit('rumps' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


def test_remove_asks_first_and_cancel_does_nothing(monkeypatch):
    rows = mc.menu_spec(Fake())
    (row,) = [r for r in _find(rows, "1  a@x.com") if r.action == "remove" and r.args[0] == "claude"]
    assert row.confirm
    answers = iter([0, 1])
    asked = []
    fake_rumps = SimpleNamespace(alert=lambda **kw: asked.append(kw) or next(answers))
    monkeypatch.setitem(sys.modules, "rumps", fake_rumps)
    calls = []
    ctl = SimpleNamespace(remove=lambda *a: calls.append(a))
    assert mc._activate(ctl, row) is False and calls == []
    assert mc._activate(ctl, row) is True and calls == [("claude", 1)]
    assert asked[0]["ok"] == "Remove" and asked[0]["cancel"] == "Cancel"


def test_health_row_restarts_failed_engine():
    f = Fake()
    assert mc.menu_spec(f)[0].action is None
    f.states["codex"].engine_failure = "engine died"
    row = mc.menu_spec(f)[0]
    assert (row.action, row.args, row.tooltip) == ("restart_engine", ("codex",), "engine died")
    f.states["codex"].engine_failure = None
    base = f.payload()
    base["providers"][0]["autoSwitch"]["failure"] = "boom"
    f.payload = lambda: base
    row = mc.menu_spec(f)[0]
    assert (row.action, row.args, row.tooltip) == ("restart_engine", ("claude",), "boom")


def test_paint_glyph_falls_back_when_the_image_cannot_be_built():
    # no rumps app / status item: must report False so the text icon stays
    assert mc._paint_glyph(object(), Fake(), True) is False


def test_use_popover_saves_then_restarts_in_the_popover(monkeypatch):
    monkeypatch.setitem(sys.modules, "rumps", SimpleNamespace(alert=lambda **kw: 1))
    row = mc.MenuRow("Use popover", action="set_display", args=("style", "popover"))
    execs, stops = [], []
    monkeypatch.setattr(mc.os, "execv", lambda path, argv: execs.append(argv))
    ctl = SimpleNamespace(set_display=lambda *a: False, stop=lambda wait: stops.append(wait))
    assert mc._activate(ctl, row) and execs == []  # the save failed: stay
    ctl.set_display = lambda *a: True
    assert mc._activate(ctl, row)
    assert stops == [10] and execs == [[sys.executable, "-m", "ccswap", "menubar"]]
