import stat

import pytest

from claude_swap.vendors.isolation import IsolatedHome


def test_home_is_private_and_removed_on_exit():
    with IsolatedHome("t") as home:
        assert stat.S_IMODE(home.stat().st_mode) == 0o700
        login = home / "muse" / "auth.json"
        IsolatedHome.write_private(login, "{}")
        assert stat.S_IMODE(login.stat().st_mode) == 0o600
    assert not home.exists()


def test_home_is_removed_when_the_body_raises():
    with pytest.raises(RuntimeError):
        with IsolatedHome("t") as home:
            raise RuntimeError("probe failed")
    assert not home.exists()
