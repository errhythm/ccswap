"""A scriptable in-process stand-in for the vendor process boundary."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from claude_swap.vendors.core import RunResult

Handler = RunResult | Callable[["Call"], RunResult]


@dataclass
class Call:
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    unset_env: tuple[str, ...] = ()
    input_text: str | None = None
    interactive: bool = False
    cwd: str | None = None


class FakeRunner:
    """Longest-prefix argv routing.  Records every call for assertions."""

    def __init__(self, binaries: Mapping[str, str] | None = None) -> None:
        self.binaries = dict(binaries or {})
        self.routes: dict[tuple[str, ...], Handler] = {}
        self.interactive_routes: dict[tuple[str, ...], Callable[[Call], int] | int] = {}
        self.calls: list[Call] = []

    def on(self, *argv: str, result: Handler) -> FakeRunner:
        self.routes[tuple(argv)] = result
        return self

    def on_interactive(self, *argv: str, code: Callable[[Call], int] | int = 0) -> FakeRunner:
        self.interactive_routes[tuple(argv)] = code
        return self

    def which(self, name: str) -> str | None:
        return self.binaries.get(name)

    def _match(self, table, argv):
        best = None
        for prefix in table:
            if tuple(argv[: len(prefix)]) == prefix and (best is None or len(prefix) > len(best)):
                best = prefix
        return table[best] if best is not None else None

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 20.0,
        env=None,
        unset_env=(),
        input_text=None,
        cwd=None,
    ) -> RunResult:
        call = Call(tuple(argv), dict(env or {}), tuple(unset_env), input_text, cwd=cwd)
        self.calls.append(call)
        handler = self._match(self.routes, argv)
        if handler is None:
            return RunResult(2, stderr="unexpected invocation")
        return handler(call) if callable(handler) else handler

    def run_interactive(self, argv: Sequence[str], *, env=None, unset_env=()) -> int:
        call = Call(tuple(argv), dict(env or {}), tuple(unset_env), None, True)
        self.calls.append(call)
        handler = self._match(self.interactive_routes, argv)
        if handler is None:
            return 2
        return handler(call) if callable(handler) else handler

    def argvs(self) -> list[tuple[str, ...]]:
        return [c.argv for c in self.calls]


def ok(stdout: str = "") -> RunResult:
    return RunResult(0, stdout=stdout)


def fail(stderr: str = "boom", code: int = 1) -> RunResult:
    return RunResult(code, stderr=stderr)
