# ccswap CLI reference

ccswap (package `claude_swap`) tracks subscription quota across Claude Code and Codex CLI accounts and switches between them. This file has the full command surface, JSON schema, error envelope, and gotchas that SKILL.md's tables leave out.

Nothing here assumes a fixed account count or any local config, an installation can have any number of Claude and Codex accounts configured.

## Command surface

Read-only:
- `list` (alias `ls`) with `--json`, `--token-status`, `--provider {claude,codex,all}`
- `status` with `--json`
- `config list`, `config get KEY`, `config path`
- `auto --once --dry-run`
- `codex list`, `codex status`, `codex usage` (all accept `--json`)

Mutating:
- `switch` (bare = rotate to next), `switch NUM|EMAIL` (jump to target)
- `add`, `add-token`, `remove`, `disable NUM|EMAIL`, `enable NUM|EMAIL`
- `map`, `unmap`, `swap`, `move`, `alias`, `export`, `import`, `purge`, `upgrade`
- `unclaimed --purge`
- `menubar --install-service`, `auto --install-service`
- `run`
- `config set`, `config unset`
- `auto` (polling loop, no `--once`)
- `codex switch [NUM|EMAIL]`, `codex auto [--once] [--dry-run]`, `codex add`, `codex remove`

`run`, `auto`, `config`, and `codex` must be the first argv token. `ccswap --debug run 2` is not supported, put flags after the subcommand.

Bare `ccswap` with no subcommand, in an interactive TTY, opens a full-screen TUI dashboard. Never call bare `ccswap` from a script, always pass a subcommand.

Running as root on POSIX is a hard error, exit 1.

## `switch` flags

| Flag | Valid with | Notes |
|---|---|---|
| `--strategy {best,next-available}` | bare `switch` only | error if combined with a target (`switch <email> --strategy` fails) |
| `--model NAMES` | only combined with `--strategy` | scopes the strategy to a per-model window |
| `--force` | `switch <target>` or `import` only | skips the already-active no-op guard and skips backing up the current login first. Recovery path for a stale live login, not a casual override, using it can lose the current session's backup |
| `--json` | any `switch` form | |

`--json` is available only on: `list`, `status`, `switch`/`switch-to`, `config get|list`, and the `codex` subcommands. Combining `--json` with `--token-status` is rejected.

## `list --json` schema (schemaVersion: 1)

```json
{
  "schemaVersion": 1,
  "activeAccountNumber": 2,
  "accounts": [ { "...": "..." } ],
  "duplicateAccountWarnings": [],
  "lockstepUsageWarnings": [],
  "unclaimedCredentials": []
}
```

Each account row: `number`, `email`, `organizationName`, `organizationUuid`, `isOrganization`, `active`, `usageStatus`, `usage`, plus optionally `alias`, `disabled`, `loginExpiresAt`, `usageFetchedAt`, `usageAgeSeconds`, `lastGoodUsage`, `lastGoodFetchedAt`, `lastGoodAgeSeconds`, `usageError`, `usageRetryAt`.

`usageStatus` values: `ok`, `token_expired`, `api_key`, `keychain_unavailable`, `relogin_required`, `foreign_credential`, `no_credentials`, `unavailable`.

### `usage` object (Claude)

The percentage key is literally `pct` (float 0-100), nested per window. There is no `pct_5h` or `five_hour_pct` field.

- `usage.fiveHour = {"pct": float, "resetsAt"?, "countdown"?, "clock"?}`, never has pace fields
- `usage.sevenDay = {"pct": float, "resetsAt"?, "countdown"?, "clock"?, "expectedPct"?, "aheadOfPace"?, "projectedExhaustionAt"?, "willLastToReset"?}`
- `usage.scoped = [{"name": "<model display name>", "pct": float, "resetsAt"?, ...}]`, per-model weekly windows. A single model can be exhausted while the account overall has headroom
- also possible: `usage.weekly`, `usage.resetCredits`, `usage.credits`, `usage.creditAllowance`, `usage.spend`

### `--provider all --json` shape

```json
{
  "claude": { "schemaVersion": 1, "activeAccountNumber": 2, "accounts": [ {"...": "usage.fiveHour / usage.sevenDay ..."} ] },
  "codex": { "activeAccountNumber": 1, "accounts": [ {"...": "usage.weekly ..."} ] }
}
```

The Codex payload nests under `.codex` and carries `usage.weekly` rather than `usage.fiveHour`, since Codex has no 5-hour window. `--provider claude` or `--provider codex` returns just that inner object (the bare payload older scripts already parse).

### Error envelope

Any `--json` command can return, on exit 1:

```json
{"schemaVersion":1,"error":{"type":"<ExceptionClassName>","message":"..."}}
```

## Headroom definition

`100 - max(pct)` across the windows that apply to the provider (`fiveHour` and `sevenDay` for Claude, `weekly` for Codex), plus any per-model `scoped` window named via `--model`. `<= 0` means at or over limit. `None`/missing means unknown, and unknown accounts are never auto-skipped, they must be surfaced.

Claude `auto` can give 5h, 7d, and named models their own thresholds (`autoswitch.threshold5h`, `autoswitch.threshold7d`, `autoswitch.modelThresholds`). Any one gate at or over its threshold switches. An unset gate inherits `autoswitch.threshold`, which is the single-threshold behavior above.

## Strategies

- `switch --strategy best`: jumps to the switchable account with the highest headroom.
- `switch --strategy next-available`: rotates, skipping accounts already at their limit.
- `auto` strategy `best` (default): fires a proactive switch when the active account crosses the threshold, ranked by headroom. Special rule: when both the active account and the best candidate have under 3 points of headroom (`SPENT_HEADROOM_PCT = 3.0`), it ranks by soonest reset instead of headroom.
- `auto` strategy `consume-first`: proactively burns down the account whose weekly window resets soonest, even below threshold.
- `codex auto` uses the same threshold, cooldown, `--once`, `--dry-run`, and JSON event mechanics as Claude `auto`, scoped to Codex's `weekly` window.

## Settings defaults (dotted keys, `ccswap config`)

| Key | Default | Valid range |
|---|---|---|
| `autoswitch.threshold` | 90.0 | 50.0-99.9 |
| `autoswitch.threshold5h` | None (inherit `threshold`) | 1.0-99.9 |
| `autoswitch.threshold7d` | None (inherit `threshold`) | 1.0-99.9 |
| `autoswitch.modelThresholds` | None | `Fable=40` or `Fable=40,Opus=60` |
| `autoswitch.intervalSeconds` | 60.0 | 15-3600 |
| `autoswitch.cooldownSeconds` | 300.0 | 0-86400 |
| `autoswitch.strategy` | "best" | |
| `autoswitch.model` | None | |

## `auto --once` exit codes

| Code | Meaning |
|---|---|
| 0 | switched |
| 1 | error |
| 2 | no action needed |
| 3 | BLOCKED, wanted to switch but no viable target, every account exhausted |
| 130 | KeyboardInterrupt |

General errors (not tied to `auto`) exit 1. `codex auto --once` follows the same exit code convention.

## Staleness and polling

Usage data is trusted for decisions up to 300 seconds old (`STALE_OK_S`). Older data is reported as `usageStatus: "unavailable"`, with `lastGoodUsage` retained for display only. Polling backs off adaptively after an HTTP 429, exposed via `usageError`/`usageRetryAt`. A tight scripted loop around `auto --once` hits the same per-account backoff as the daemon, do not loop it faster than the daemon would.

## Restart behavior per provider

- **Claude Code**: On Linux and Windows the credentials file is re-read live, no restart needed. On macOS, only the ~30 second Keychain cache needs to expire; a restart just makes the switch apply instantly rather than after that delay.
- **Codex CLI**: Codex reads `auth.json` once at process start and keeps the login in memory for the life of that process, it never re-reads the file and there is no cache timer. A running Codex session keeps using the old account no matter what `ccswap codex switch` writes underneath it. This means Codex ALWAYS needs a manual restart (quit and relaunch the CLI, or reload the IDE extension) after a Codex switch, unlike Claude Code which usually does not. There is no flag, signal, or config setting that makes a live Codex process reload credentials.

## Other gotchas

- `disable`/`enable` only affect auto-rotation eligibility, they do not touch the live credential.
- Bare `ccswap` in an interactive TTY opens a TUI dashboard. Never call bare `ccswap` from a script, always pass a subcommand.
- `--model NAMES` on `switch`/`auto` matches Anthropic's per-model `display_name`s, case-insensitively, read the exact strings for the account from its `usage.scoped[].name` rows.
- API-key accounts have no subscription quota window, they show no usage and usage-aware strategies never skip them as rate-limited.
