"""``ccswap vendors`` / ``ccswap muse|opencode|kiro`` command line.

Exit status: 0 success, 1 error, 2 usage error (argparse), 3 the vendor product
cannot do what was asked (see ``docs/vendor-compatibility.md``).  With
``--json`` every handled failure prints the project's error envelope on stdout.
Secrets are only ever read from stdin or a hidden prompt, never from argv.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Sequence
from typing import Any

from claude_swap import printer
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.json_output import error_envelope
from claude_swap.vendors.core import (
    AccountView,
    UnsupportedCapability,
    UsageResult,
    UsageState,
    VendorError,
)
from claude_swap.vendors.service import VendorService

TOOLS = ("muse", "opencode", "kiro")


def _prog() -> str:
    from claude_swap.cli import _prog_name

    return _prog_name()


def read_secret(prompt: str, *, from_stdin: bool) -> str:
    """Never from argv: a pipe/redirect, or a no-echo prompt on a terminal."""
    if sys.stdin.isatty():
        value = getpass.getpass(prompt)  # hidden, even when --api-key-stdin was requested
    else:
        value = sys.stdin.readline()
    return value.strip()


def confirm(question: str) -> bool:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ---- rendering -------------------------------------------------------------


def _active_mark(view: AccountView) -> str:
    return {True: "*", False: " ", None: "?"}[view.active]


def render_accounts(tool: str, views: Sequence[AccountView]) -> None:
    if not views:
        print(f"No {tool} accounts. Run `{_prog()} {tool} add` to register one.")
        return
    print("  (* active, ? the vendor cannot say)")
    for number, v in enumerate(views, 1):
        who = v.alias
        details = [v.kind]
        if v.provider:
            details.insert(0, v.provider)
        email = v.identity.get("email")
        if email:
            details.append(email)
        reg = "" if v.registered else " [unregistered]"
        note = f" -- {v.note}" if v.note else ""
        print(f" {_active_mark(v)} {number}. {who}  ({', '.join(details)}){reg}{note}")


def render_usage(result: UsageResult) -> None:
    from claude_swap.vendors.overview import render_usage_lines

    lines = render_usage_lines(result, indent="")
    print("\n".join(lines) if lines else f"Usage {result.state.value}")


def render_status(payload: dict) -> None:
    ver = payload.get("version") or "unknown version"
    if not payload.get("installed"):
        print(f"{payload['label']}: not installed")
    else:
        print(f"{payload['label']}: {ver} at {payload.get('path')}")
    if payload.get("error"):
        print(f"  error: {payload['error']}")
    auth = payload.get("auth") or {}
    for key, value in auth.items():
        print(f"  {key}: {value}")
    print(f"  registered accounts: {payload.get('accounts', 0)}")
    for name, status in (payload.get("capabilities") or {}).items():
        print(f"  {name:<22} {status['support']:<12} {status['mechanism']}")


# ---- parser ----------------------------------------------------------------


def _parser(tool: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{_prog()} {tool}")
    sub = parser.add_subparsers(dest="action", required=True)

    def with_json(p):
        p.add_argument("--json", action="store_true", help="Machine-readable output")
        return p

    for name in ("list", "status"):
        with_json(sub.add_parser(name, help=f"{name.capitalize()} {tool} accounts"))
    if tool == "opencode":
        with_json(sub.add_parser("accounts", help="Alias of list"))

    usage = with_json(sub.add_parser("usage", help="Show verified usage, or why it is unavailable"))
    usage.add_argument("account", nargs="?", metavar="ALIAS|NUM")
    usage.add_argument("--refresh", action="store_true", help="Ignore the 60 s cache")
    if tool == "opencode":
        usage.add_argument(
            "--local", action="store_true", help="Local token/cost statistics (not a quota)"
        )

    add = with_json(sub.add_parser("add", help="Register an account (no vendor login is changed)"))
    if tool == "opencode":
        add.add_argument("alias")
        add.add_argument("--provider", required=True)
        add.add_argument("--account", help="OpenCode account label or id")
    else:
        add.add_argument("alias")
        if tool == "muse":
            add.add_argument(
                "--browser",
                action="store_true",
                help="Register the current browser login (ccswap keeps it so it can switch back)",
            )
            add.add_argument(
                "--from-file",
                metavar="PATH",
                help="With --browser: a saved Muse login file instead",
            )
        else:
            add.add_argument(
                "--api-key",
                action="store_true",
                help="Register a KIRO_API_KEY (read from stdin/prompt)",
            )
            add.add_argument("--social", choices=("google", "github"))
            add.add_argument("--license", choices=("free", "pro"))
            add.add_argument("--identity-provider", metavar="URL")
            add.add_argument("--region")
            add.add_argument("--device-flow", action="store_true")
            add.add_argument(
                "--profile-arn",
                metavar="ARN",
                help="Register another Kiro profile of the current login (e.g. a second AWS "
                "account in your organization); see `kiro-cli profile`",
            )
            add.add_argument("--profile-name", help="Display name of that profile")
        add.add_argument("--api-key-stdin", action="store_true", help="Read the key from stdin")

    remove = with_json(
        sub.add_parser("remove", help="Forget a registration (vendor login untouched)")
    )
    remove.add_argument("account", metavar="ALIAS|NUM")
    if tool == "opencode":
        remove.add_argument("--provider")

    switch = with_json(
        sub.add_parser("switch", help="Make an account active using the vendor's own mechanism")
    )
    if tool == "opencode":
        switch.add_argument("provider")
        switch.add_argument("account", metavar="ACCOUNT")
    else:
        switch.add_argument("account", metavar="ALIAS|NUM")
        switch.add_argument(
            "--login", action="store_true", help="Allow the interactive vendor login flow"
        )
        switch.add_argument(
            "--yes", action="store_true", help="Confirm replacing the vendor's current login"
        )

    if tool in ("muse", "kiro"):
        run = sub.add_parser(
            "run", help="Run the tool as one account, without changing global login state"
        )
        run.add_argument(
            "account",
            metavar="ALIAS|NUM|@key",
            help="@key: the first registered API-key account (what crew lanes run as)",
        )
        run.add_argument(
            "args", nargs=argparse.REMAINDER, help="Arguments after `--` go to the tool"
        )
    if tool == "opencode":
        login = sub.add_parser("login", help="Run `opencode auth login` (interactive)")
        login.add_argument("provider", nargs="?")
        logout = sub.add_parser(
            "logout", help="Run `opencode auth logout` (removes the vendor credential)"
        )
        logout.add_argument("provider")
        logout.add_argument("account", nargs="?")
        logout.add_argument("--yes", action="store_true")
    return parser


def _fail(exc: Exception, as_json: bool) -> int:
    if isinstance(exc, ClaudeSwitchError):
        message = str(exc)
        code = getattr(exc, "exit_code", 1)
    else:
        message, code = "Cannot access vendor account storage", 1
        exc = VendorError(message)
    if as_json:
        print(json.dumps(error_envelope(exc)))
    else:
        printer.error(f"Error: {message}")
    return code


# ---- dispatch ----------------------------------------------------------------


def tool_command(tool: str, argv: list[str], service: VendorService | None = None) -> int:
    args = _parser(tool).parse_args(argv)
    service = service or VendorService()
    as_json = bool(getattr(args, "json", False))
    try:
        return _dispatch(tool, args, service, as_json)
    except (ClaudeSwitchError, OSError) as exc:
        return _fail(exc, as_json)
    except KeyboardInterrupt:
        hint = (
            " If you were switching a Kiro login, run `kiro-cli login` to sign in again."
            if tool == "kiro"
            else ""
        )
        printer.error(f"Interrupted.{hint}")
        return 130


def _emit(payload: dict, as_json: bool, human) -> int:
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        human()
    return 0


def _dispatch(tool: str, args, service: VendorService, as_json: bool) -> int:
    action = args.action
    adapter = service.adapter(tool)
    if action in ("list", "accounts"):
        views = service.list_views(tool)
        return _emit(
            service.list_payload(tool, views), as_json, lambda: render_accounts(tool, views)
        )
    if action == "status":
        payload = service.status_payload(tool)
        return _emit(payload, as_json, lambda: render_status(payload))
    if action == "usage":
        if getattr(args, "local", False):
            result = adapter.local_stats()  # type: ignore[attr-defined]
            payload = {"schemaVersion": 1, "tool": tool, **result.to_json()}
            return _emit(payload, as_json, lambda: render_usage(result))
        if args.account:
            result = service.usage(tool, args.account, refresh=args.refresh)
            payload = service.usage_envelope(tool, args.account, result)
            return _emit(payload, as_json, lambda: render_usage(result))
        from claude_swap.vendors.overview import Overview, account_json, render_summary

        summary = Overview(service, legacy=[]).collect_one(tool, refresh=args.refresh)
        tool_usage = summary.tool_usage or UsageResult(UsageState.UNAVAILABLE, "not installed")
        payload = service.usage_envelope(tool, None, tool_usage)
        payload["accounts"] = [account_json(v, u) for v, u in summary.accounts]
        return _emit(payload, as_json, lambda: print("\n".join(render_summary(summary))))
    if action == "add":
        options: dict = {}
        if tool == "opencode":
            options = {"provider": args.provider, "account": args.account}
        elif tool == "muse":
            options = {
                "kind": "browser" if args.browser else "api-key",
                "from_file": args.from_file,
            }
        else:
            options = {
                "kind": "api-key" if args.api_key else "login",
                "social": args.social,
                "license": args.license,
                "identity_provider": args.identity_provider,
                "region": args.region,
                "device_flow": args.device_flow,
                "profile_arn": args.profile_arn,
                "profile_name": args.profile_name,
            }
        if options.get("kind") == "api-key":
            options["api_key"] = read_secret(
                f"{adapter.label} API key: ", from_stdin=args.api_key_stdin
            )
        record = service.add(tool, args.alias, **options)
        return _emit(
            {"schemaVersion": 1, "tool": tool, "account": record.to_json()},
            as_json,
            lambda: print(
                f"Registered {tool} account '{record.alias}' ({record.kind}). "
                "No vendor login was changed."
            ),
        )
    if action == "remove":
        record = service.remove(tool, args.account, getattr(args, "provider", None))
        return _emit(
            {"schemaVersion": 1, "tool": tool, "removed": record.alias},
            as_json,
            lambda: print(
                f"Removed '{record.alias}' from ccswap. Your {adapter.label} login was not touched."
            ),
        )
    if action == "switch":
        if tool == "opencode":
            outcome, _ = service.switch(tool, args.account, provider=args.provider)
        else:
            outcome, _ = service.switch(
                tool,
                args.account,
                assume_yes=args.yes,
                interactive=args.login
                and sys.stdin.isatty()
                and sys.stdout.isatty()
                and not as_json,
            )
        payload = {"schemaVersion": 1, "tool": tool, **outcome.to_json()}

        def human() -> None:
            print(outcome.message)
            for note in outcome.warnings:
                printer.warning(note)
            if outcome.restart_required:
                print("Restart required: running sessions keep the previous account.")

        return _emit(payload, as_json, human)
    if action == "run":
        extra = args.args[1:] if args.args[:1] == ["--"] else args.args
        account = args.account
        if account == "@key":
            keys = [r.alias for r in service.store.records(tool) if r.kind == "api-key"]
            if not keys:
                raise VendorError(
                    f"No {tool} API-key account is registered; refusing to run on the global login"
                )
            account = keys[0]
        return service.run(tool, account, extra)
    if action == "login":
        return adapter.login(args.provider)  # type: ignore[attr-defined]
    if action == "logout":
        if not args.yes and not confirm(
            f"Remove the {args.provider} credential from OpenCode itself?"
        ):
            raise VendorError(
                "Refusing to log out without --yes (this deletes OpenCode's credential)"
            )
        return adapter.logout(args.provider, args.account)  # type: ignore[attr-defined]
    raise UnsupportedCapability(f"{tool} does not support '{action}'")


# ---- ccswap vendors ----------------------------------------------------------


def vendors_command(argv: list[str], service: VendorService | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"{_prog()} vendors")
    sub = parser.add_subparsers(dest="action")
    for name in ("status", "capabilities"):
        p = sub.add_parser(name)
        # SUPPRESS: a subparser default would overwrite the parent's `vendors --json`.
        p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        if name == "capabilities":
            p.add_argument("tool", nargs="?", choices=TOOLS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    service = service or VendorService()
    as_json = bool(getattr(args, "json", False))
    try:
        if args.action == "capabilities":
            adapters = [service.adapter(args.tool)] if args.tool else service.registry.adapters()
            payload: dict[str, Any] = {
                "schemaVersion": 1,
                "vendors": {a.key: service.capability_table(a) for a in adapters},
            }
            return _emit(
                payload,
                as_json,
                lambda: [
                    print(
                        f"{a.label}\n"
                        + "\n".join(
                            f"  {n:<22} {s['support']:<12} {s['mechanism']}"
                            for n, s in payload["vendors"][a.key].items()
                        )
                    )
                    for a in adapters
                ],
            )
        payload = service.vendors_payload()

        def human() -> None:
            for v in payload["vendors"]:
                state = f"{v.get('version') or '?'}" if v.get("installed") else "not installed"
                print(
                    f"{v['label']:<12} {state:<14} accounts: {v.get('accounts', 0)}"
                    + (f"  ! {v['error']}" if v.get("error") else "")
                )

        return _emit(payload, as_json, human)
    except (ClaudeSwitchError, OSError) as exc:
        return _fail(exc, as_json)


# ---- ccswap usage (all tools) / ccswap claude --------------------------------


def usage_problems(summaries, env) -> list[str]:
    """Why the menu bar could not show every registered account's numbers, if anything.

    An empty list is the install gate's pass.  Muse needs one credential backend for
    every process: without ``TBH_CREDENTIAL_BACKEND=file`` a Muse started from here
    rewrites the login into a Keychain pointer that file-backend Muse cannot read.
    """
    problems = []
    for summary in summaries:
        if not summary.installed:
            continue
        backend = env.get("TBH_CREDENTIAL_BACKEND")
        if summary.tool == "muse" and summary.accounts and backend != "file":
            problems.append(
                "muse: TBH_CREDENTIAL_BACKEND is not 'file' in this environment "
                "(launchctl getenv TBH_CREDENTIAL_BACKEND)"
            )
        for view, usage in summary.accounts:
            if view.registered and usage.state is not UsageState.AVAILABLE:
                detail = usage.reason or usage.error or usage.state.value
                problems.append(f"{summary.tool}: {view.alias}: {usage.state.value}: {detail}")
    return problems


def usage_command(argv: list[str], overview=None) -> int:
    """``ccswap usage [TOOL ...] [--json] [--refresh]``: every tool, every account."""
    from claude_swap.vendors.overview import Overview, overview_json, render_summary

    overview = overview or Overview()
    parser = argparse.ArgumentParser(
        prog=f"{_prog()} usage",
        description="Usage, remaining allowance and reset times for every AI coding tool "
        "and each of its accounts, in one pass. Unknown values are shown as unknown.",
    )
    parser.add_argument(
        "tools", nargs="*", metavar="TOOL", help=f"limit to: {', '.join(overview.tools())}"
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument("--refresh", action="store_true", help="Bypass short-lived caches")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Measure every account now and exit 1 if any cannot be shown (install gate)",
    )
    args = parser.parse_args(argv)
    unknown = [t for t in args.tools if t not in overview.tools()]
    if unknown:
        return _fail(VendorError(f"Unknown tool: {', '.join(unknown)}"), args.json)
    try:
        summaries = overview.collect(args.tools or None, refresh=args.refresh or args.check)
    except KeyboardInterrupt:
        printer.error("Interrupted.")
        return 130
    if args.check:
        problems = usage_problems(summaries, os.environ)
        for problem in problems:
            printer.error(problem)
        if not problems:
            print(f"ok: every account of {len(summaries)} tools has a usage reading")
        return 1 if problems else 0
    if args.json:
        print(json.dumps(overview_json(summaries), indent=2))
        return 0
    for index, summary in enumerate(summaries):
        if index:
            print()
        for line in render_summary(summary):
            print(line)
    return 0


# Claude commands that already live at the top level and pass through unchanged.  Other
# tools' namespaces (codex, kiro, ...) are deliberately not reachable via `claude`.
def _claude_passthrough() -> frozenset[str]:
    """Every established top-level Claude verb (derived, so new ones are included)."""
    from claude_swap.cli import _SUBCOMMAND_FLAGS

    special = {
        "switch",
        "run",
        "auto",
        "config",
        "map",
        "unmap",
        "unclaimed",
        "alias",
        "swap",
        "move",
    }
    return frozenset({*_SUBCOMMAND_FLAGS, *special} - {"help"})


_CLAUDE_VERBS = {
    "list": ["--list", "--provider", "claude"],
    "ls": ["--list", "--provider", "claude"],
    "usage": ["--list", "--provider", "claude"],
}


def claude_argv(argv: list[str]) -> list[str]:
    """``ccswap claude <verb> ...`` -> the established top-level Claude command line.

    Claude Code is ccswap's original tool, so its commands already live at the top level
    (``ccswap switch``, ``ccswap add``, ...); this namespace only makes it a peer of
    ``ccswap codex|muse|kiro|opencode`` without changing any of them.
    """
    if not argv or argv[0] in ("-h", "--help", "help"):
        return ["--help"]
    verb, rest = argv[0], argv[1:]
    if verb in _CLAUDE_VERBS:
        return [*_CLAUDE_VERBS[verb], *rest]
    passthrough = _claude_passthrough()
    if verb.startswith("-") or verb in passthrough:
        return argv
    raise ValueError(
        f"'{verb}' is not a Claude Code command. Try: ccswap claude "
        + "|".join(sorted({*_CLAUDE_VERBS, *passthrough}))
    )
