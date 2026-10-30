# ccswap (Claude Codex Swap)

[![CI](https://github.com/errhythm/ccswap/actions/workflows/ci.yml/badge.svg)](https://github.com/errhythm/ccswap/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ccswap.svg)](https://pypi.org/project/ccswap/)
[![Python versions](https://img.shields.io/pypi/pyversions/ccswap.svg)](https://pypi.org/project/ccswap/)
[![Downloads](https://img.shields.io/pypi/dm/ccswap.svg)](https://pypi.org/project/ccswap/)
[![License](https://img.shields.io/pypi/l/ccswap.svg)](https://github.com/errhythm/ccswap/blob/main/LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/errhythm/ccswap?style=social)](https://github.com/errhythm/ccswap)

Multi-account and usage manager for Claude Code and OpenAI Codex. Save multiple logins, check their quota windows, switch manually or automatically before you hit a rate limit, and manage both providers from one dashboard.

<img src="https://raw.githubusercontent.com/errhythm/ccswap/main/assets/tui-watch.png" width="760" alt="ccswap watch: live usage bars for every Claude Code and Codex account, with reset times and the active account marked">

## What it does

- Keeps several Claude Code and Codex CLI logins side by side and switches the active one with `ccswap switch 2` or `ccswap codex switch 2`.
- Shows every account's 5-hour, 7-day and Codex weekly usage with reset times in `ccswap list`.
- `ccswap auto` switches to the account with the most quota left once the active one crosses 90%, before the rate limit hits.
- `ccswap run 2` starts Claude Code as account 2 in one terminal only, so two accounts work in parallel.
- A full-screen dashboard (`ccswap`) on macOS, Linux and Windows, and an optional macOS menu bar app.
- `--json` output and exit codes for scripts and cron.

## Quick start

```bash
uv tool install ccswap     # or: pipx install ccswap

# Log into Claude Code with your first account, then:
ccswap add
# Log in with the next account, then:
ccswap add

ccswap list       # every account's usage and reset times
ccswap switch 2   # or: ccswap switch --strategy best
ccswap auto       # or let it switch for you before a limit is hit
```

Codex accounts work the same way: run `codex login`, then `ccswap codex add`.

You usually don't need to restart Claude Code after a switch. Codex always needs one. See [restarts](https://github.com/errhythm/ccswap/blob/main/docs/how-it-works.md#restarts-and-session-behavior).

## Common tasks

**Switch by quota.** `ccswap switch --strategy best` picks the account with the most quota left. `--strategy consume-first` picks the one whose weekly limit resets soonest, so none of it expires unused. [All switch options](https://github.com/errhythm/ccswap/blob/main/docs/commands.md#switch-accounts)

**Switch automatically.** `ccswap auto` polls every 60 seconds and is safe to run while Claude Code works. Thresholds, per-model limits, cron mode and strategies are in [auto-switching](https://github.com/errhythm/ccswap/blob/main/docs/auto-switching.md).

Optional [five-hour window warming](docs/auto-switching.md#optional-five-hour-window-warming) sends a minimal Haiku prompt for idle Claude accounts. It uses subscription quota and is disabled by default.

**Two accounts at once.** `ccswap run 2` launches Claude Code as account 2 in this terminal only. `ccswap map 2 ~/work` makes a bare `ccswap run` pick account 2 in that folder. [Session mode](https://github.com/errhythm/ccswap/blob/main/docs/session-mode.md)

**Codex sessions.** `ccswap codex run 2` launches an account in a private home; the default login runs plain Codex. [Codex session mode](https://github.com/errhythm/ccswap/blob/main/docs/codex.md#session-mode-codex-run)

**Codex.** `ccswap codex list`, `codex switch` and `codex auto` mirror the Claude commands. Codex must use its file credential store. [Codex accounts](https://github.com/errhythm/ccswap/blob/main/docs/codex.md)

**Dashboard and menu bar.** Run `ccswap` for the dashboard, or `ccswap watch` for the live monitor above. On macOS, `ccswap menubar --install-service` keeps a menu bar app running at login. [Dashboard and menu bar](https://github.com/errhythm/ccswap/blob/main/docs/dashboard.md)

**Inside Claude Code.** An optional statusline and a `/ccswap` pane, both off until you turn them on. [Statusline and mod](https://github.com/errhythm/ccswap/blob/main/docs/statusline-and-mod.md)

## Documentation

- [Commands](https://github.com/errhythm/ccswap/blob/main/docs/commands.md): every command, expired tokens, adding a raw token or API key
- [Automatic switching](https://github.com/errhythm/ccswap/blob/main/docs/auto-switching.md)
- [Session mode](https://github.com/errhythm/ccswap/blob/main/docs/session-mode.md)
- [Codex accounts](https://github.com/errhythm/ccswap/blob/main/docs/codex.md)
- [Dashboard and menu bar](https://github.com/errhythm/ccswap/blob/main/docs/dashboard.md)
- [Statusline and mod](https://github.com/errhythm/ccswap/blob/main/docs/statusline-and-mod.md)
- [Configuration, backup and sharing](https://github.com/errhythm/ccswap/blob/main/docs/configuration.md)
- [JSON output for scripting](https://github.com/errhythm/ccswap/blob/main/docs/json.md)
- [How it works](https://github.com/errhythm/ccswap/blob/main/docs/how-it-works.md): restarts, credential handling, where data is stored

## Agent skill

`skills/ccswap-orchestration/` teaches a coding agent to check account headroom before a big fan-out and to pick a switch strategy under quota pressure:

```bash
npx skills add errhythm/ccswap --skill ccswap-orchestration
```

## Updating and uninstalling

```bash
ccswap upgrade        # detects uv or pipx and upgrades

ccswap purge          # remove all ccswap data, then:
uv tool uninstall ccswap    # or: pipx uninstall ccswap
```

From source: `git clone https://github.com/errhythm/ccswap.git && cd ccswap && uv sync && uv run ccswap help`.

## Requirements

- Python 3.12+
- Claude Code and/or Codex CLI
- A file-backed login for managed Codex accounts

## Support

If ccswap saved you from a rate limit, a ⭐ on [GitHub](https://github.com/errhythm/ccswap) helps other people find it. Bugs and ideas go in [issues](https://github.com/errhythm/ccswap/issues).

## License and credits

`ccswap` began as a fork of [claude-swap (`cswap`)](https://github.com/realiti4/claude-swap) by Onur Cetinkol, and still carries the original MIT license and credit for that. Since then it's grown into its own project: no more tracking upstream, no `cswap` compatibility, its own package and release line, and adds Codex support. It's MIT-licensed too, so fork it, file issues, send PRs — whatever's useful to you.

MIT. This fork retains the original project's copyright and license notice; see [LICENSE](https://github.com/errhythm/ccswap/blob/main/LICENSE). Upstream: [realiti4/claude-swap](https://github.com/realiti4/claude-swap).
