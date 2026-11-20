"""The provider registry: capability flags must match the real classes."""

from __future__ import annotations

import dataclasses
import subprocess
import sys

import pytest

from ccswap import providers
from ccswap.providers import (
    DisablingProvider,
    RotatingProvider,
    SwitchingProvider,
    UsageProvider,
)


def _protocol_methods(proto) -> dict[str, object]:
    return {
        name: getattr(proto, name)
        for name in proto.__protocol_attrs__
    }


def _provider_class(spec):
    # The factory imports lazily; calling it with the class's own ctor is
    # avoided (it would touch the real store), so resolve the class by name.
    module = {"claude": "switcher", "codex": "codex", "agy": "agy"}[spec.id]
    cls_name = {
        "claude": "ClaudeAccountSwitcher",
        "codex": "CodexAccountSwitcher",
        "agy": "AgyAccountSwitcher",
    }[spec.id]
    mod = __import__(f"ccswap.{module}", fromlist=[cls_name])
    return getattr(mod, cls_name)


def test_ids_unique_and_ordered():
    assert list(providers.PROVIDERS) == ["claude", "codex", "agy"]
    assert all(k == s.id for k, s in providers.PROVIDERS.items())
    assert providers.get("codex").label == "Codex"
    with pytest.raises(KeyError):
        providers.get("nope")


def test_specs_are_frozen():
    spec = providers.get("claude")
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.label = "x"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.capabilities.switch = False  # type: ignore[misc]


@pytest.mark.parametrize("pid", list(providers.PROVIDERS))
def test_capabilities_match_real_methods(pid):
    spec = providers.get(pid)
    cls = _provider_class(spec)
    caps = spec.capabilities
    required = [UsageProvider]
    if caps.switch:
        required.append(SwitchingProvider)
    if caps.strategies:
        required.append(RotatingProvider)
    if caps.disable:
        required.append(DisablingProvider)
    for proto in required:
        for name in _protocol_methods(proto):
            assert callable(getattr(cls, name, None)), f"{pid} lacks {name}"
    if caps.strategies:
        assert caps.switch
    if caps.auto_switch:
        assert hasattr(cls, "clear_poll_policy_inputs")
    if caps.plans:
        assert hasattr(cls, "refresh_subscription_plans")
    if caps.add_token:
        assert hasattr(cls, "add_account_from_token")


def test_protocol_methods_accept_the_documented_arguments():
    import inspect

    for spec in providers.PROVIDERS.values():
        cls = _provider_class(spec)
        params = inspect.signature(cls.remove_account).parameters
        assert {"identifier", "assume_yes"} <= set(params)
        assert "fetch" in inspect.signature(cls.accounts_snapshot).parameters
        if spec.capabilities.switch:
            sp = inspect.signature(cls.switch_to).parameters
            assert {"identifier", "json_output"} <= set(sp)


def test_import_is_lazy():
    code = (
        "import sys, ccswap.providers\n"
        "bad = [m for m in ('ccswap.switcher', 'ccswap.codex', 'ccswap.agy')"
        " if m in sys.modules]\n"
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
