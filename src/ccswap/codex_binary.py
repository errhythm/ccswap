"""Internal native binary resolution and credential-free compatibility probes."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path

from ccswap.exceptions import SessionError

_MIN_VERSION = (0, 164, 0)
_STABLE_VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
_PACKAGE_VERSION = _STABLE_VERSION + r"(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"
_LIMIT = 128 * 1024
_TIMEOUT = 5
_LAYOUT = "Cannot resolve the Codex installation. Put the native codex.exe on PATH."
_TARGETS = {
    "x64": "x86_64-pc-windows-msvc",
    "arm64": "aarch64-pc-windows-msvc",
}


def _metadata(package: Path, name: str, *, expected: str | None = None) -> str:
    data = json.loads((package / "package.json").read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("name") != name
            or not isinstance(data.get("version"), str)
            or (data["version"] != expected if expected is not None
                else re.fullmatch(_PACKAGE_VERSION, data["version"]) is None)):
        raise SessionError(_LAYOUT)
    return data["version"]


def _platform_package(package: Path, arch: str) -> Path | None:
    # createRequire(bin/codex.js): bin/node_modules first, then each parent,
    # skipping node_modules/node_modules, as Node's module path algorithm does.
    for parent in (package / "bin", package, *package.parents):
        if parent.name == "node_modules":
            continue
        candidate = parent / "node_modules" / "@openai" / f"codex-win32-{arch}"
        try:
            (candidate / "package.json").stat()
        except FileNotFoundError:
            continue
        return candidate.resolve(strict=True)
    return None


def _node_arch(shim: Path, env: Mapping[str, str]) -> str:
    # npm's Windows shim prefers node.exe beside itself, then PATH's Node.
    node = shim.parent / "node.exe"
    try:
        node.lstat()
    except FileNotFoundError:
        found = shutil.which("node.exe", path=env.get("PATH", ""))
        if not found:
            raise SessionError(_LAYOUT)
        node = Path(found)
    with node.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise SessionError(_LAYOUT)
        offset = int.from_bytes(header[60:64], "little")
        if not 64 <= offset <= 1024 * 1024:
            raise SessionError(_LAYOUT)
        stream.seek(offset)
        pe = stream.read(6)
    if len(pe) != 6 or pe[:4] != b"PE\0\0":
        raise SessionError(_LAYOUT)
    machine = int.from_bytes(pe[4:6], "little")
    arch = {0x8664: "x64", 0xAA64: "arm64"}.get(machine)
    if arch is None:
        raise SessionError(_LAYOUT)
    return arch


def resolve_codex_binary(env: Mapping[str, str]) -> list[str]:
    """Resolve one PATH installation, without ever executing Windows shims."""
    try:
        found = shutil.which("codex", path=env.get("PATH", ""))
        if not found:
            raise SessionError(_LAYOUT)
        binary = Path(found).resolve(strict=True)
        if not binary.is_file():
            raise SessionError(_LAYOUT)
        if os.name != "nt":
            return [str(binary)]
        if binary.name.casefold() == "codex.exe":
            return [str(binary)]
        if binary.suffix.casefold() not in {".cmd", ".bat", ".ps1"}:
            raise SessionError(_LAYOUT)
        package = (binary.parent / "node_modules" / "@openai" / "codex").resolve(strict=True)
        version = _metadata(package, "@openai/codex")
        if not (package / "bin" / "codex.js").is_file():
            raise SessionError(_LAYOUT)
        arch = _node_arch(binary, env)
        platform = _platform_package(package, arch)
        if platform is not None:
            # npm aliases use @openai/codex with a platform-suffixed version.
            _metadata(platform, "@openai/codex", expected=f"{version}-win32-{arch}")
        root = platform if platform is not None else package
        executable = root / "vendor" / _TARGETS[arch] / "bin" / "codex.exe"
        # A resolved but broken installation must never select another arch/root.
        if not executable.is_file():
            raise SessionError(_LAYOUT)
        return [str(executable.resolve(strict=True))]
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise SessionError(_LAYOUT) from exc


def probe_codex(argv_prefix: list[str]) -> None:
    """Require the supported version and root no-daemon capability in an empty home."""
    if not argv_prefix or any(not isinstance(arg, str) or not arg for arg in argv_prefix):
        raise SessionError("Invalid Codex binary prefix.")
    # Windows probes run only the resolver's native executable, never Node/shims:
    # the parser-only --help/--version paths spawn no grandchildren, so kill() suffices.
    if os.name == "nt" and (len(argv_prefix) != 1 or
                            Path(argv_prefix[0]).name.casefold() != "codex.exe"):
        raise SessionError(_LAYOUT)
    try:
        with tempfile.TemporaryDirectory(prefix="ccswap-probe-") as directory:
            env = {key: value for key, value in os.environ.items()
                   if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
            env.update(HOME=directory, USERPROFILE=directory, CODEX_HOME=directory)
            for option in ("--version", "--help"):
                output = _probe_output(argv_prefix + [option], env)
                if option == "--version":
                    match = re.fullmatch(rb"codex-cli (" + _STABLE_VERSION.encode() + rb")\s*", output)
                    if match is None or tuple(map(int, match[1].split(b"."))) < _MIN_VERSION:
                        raise SessionError("Session mode requires Codex 0.164.0 or newer (stable).")
                elif re.search(rb"(?<![\w-])--no-daemon(?![\w-])", output) is None:
                    raise SessionError("Codex does not advertise --no-daemon.")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise SessionError("Codex compatibility probe failed.") from exc


def _kill_probe(process: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass  # The whole group/process has already exited.


def _probe_output(argv: list[str], env: dict[str, str]) -> bytes:
    # Read pipes incrementally: neither retained output nor a temporary output file
    # can grow without bound. communicate()/run(capture_output=True) cannot do this.
    chunks = bytearray()
    oversized = threading.Event()
    with subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                          # Codex warns on stderr about a temporary home; only stdout is parsed.
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          shell=False, bufsize=0, start_new_session=os.name == "posix") as process:
        def drain() -> None:
            try:
                while block := process.stdout.read(4096):
                    if len(chunks) + len(block) > _LIMIT:
                        oversized.set()
                        _kill_probe(process)
                        return
                    chunks.extend(block)
            except OSError:
                oversized.set()  # An incomplete read cannot prove compatibility.
                _kill_probe(process)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            result = process.wait(timeout=_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_probe(process)
            process.wait()
            raise
        finally:
            reader.join(timeout=_TIMEOUT)
        if reader.is_alive():
            _kill_probe(process)
            process.wait()
            reader.join(timeout=_TIMEOUT)
            raise SessionError("Codex compatibility probe failed.")
        if oversized.is_set() or result:
            raise SessionError("Codex compatibility probe failed.")
    return bytes(chunks)
