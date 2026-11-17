"""The old ``python -m claude_swap`` entry point still starts ccswap (launchd plists and app bundles from < 0.54 use it)."""

import subprocess
import sys

from ccswap import __version__


def test_python_dash_m_claude_swap_runs_ccswap():
    result = subprocess.run(
        [sys.executable, "-m", "claude_swap", "--version"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert __version__ in result.stdout
