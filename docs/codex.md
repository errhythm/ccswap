# Codex CLI accounts

`ccswap` can also save and switch Codex CLI logins. Log into each account with `codex login`, then save it before logging into the next one:

```bash
codex login
ccswap codex add

# Log in to the next Codex account, then save it too.
codex login
ccswap codex add

ccswap codex list                 # accounts tagged by plan, e.g. [Codex Team]
ccswap codex usage                # Quota, credit allowance, and banked resets
ccswap codex switch 1
ccswap codex switch                 # rotate to the next saved account
ccswap codex switch --strategy best
ccswap codex switch --strategy next-available
ccswap codex switch --strategy consume-first
ccswap codex auto --once            # switch when active quota reaches the threshold
ccswap codex remove 2
```

Usage-aware strategies consider switchable, enabled ChatGPT accounts only; API-key accounts have no subscription quota and are skipped.

Codex switching preserves the rest of `CODEX_HOME` (configuration, skills, sessions, and history) and replaces only `auth.json`. Restart Codex after switching so its running process loads the selected login. For ChatGPT-backed file logins, the dashboard reads the same read-only Codex rate-limit endpoints used by Codex and shows the weekly window with a live reset countdown, the workspace/purchased credit balance, the per-user credit allowance when the service reports one, plus the number of banked resets and the earliest available reset's expiry. Credits, per-user allowances, and banked resets are shown separately because they are different resources. Window labels are derived from the duration reported by Codex, so older accounts that still return a 5-hour window remain correctly labelled. Each account is labelled by its ChatGPT plan (`Codex Team`, `Codex Pro`, `Codex Plus`, …) — read from the login token, the closest local equivalent to Claude Code's org name, since Codex stores no workspace name on disk. API-key accounts have no ChatGPT subscription quota, so they remain status-only. `ccswap codex auto` honors `autoswitch.strategy` (`best` or `consume-first`) and the per-window thresholds `autoswitch.threshold5h` and `autoswitch.threshold7d` (Codex Weekly), and uses the same threshold, cooldown, `--once`, `--dry-run`, and JSON event controls as Claude auto-switching; it prepares the account for the next Codex launch and reports when a restart is needed.

Banked resets show a countdown and the earliest expiry in this machine's local time, including its date, timezone and UTC offset. During the last 24 hours the dashboard highlights the expiry, with stronger emphasis in the last hour; compact accounts put it first. An elapsed expiry asks you to refresh usage rather than assuming the card is still available. Known expiry warnings remain visible during usage errors, marked as the last known reading. This display never redeems a reset.

Codex must use its documented file credential store. If your `~/.codex/config.toml` says `cli_auth_credentials_store = "keyring"`, change it to `"file"`, run `codex login`, then add the account. This deliberate restriction avoids writing a guessed OS-keyring entry.

## Session mode (`codex run`)

Run a saved ChatGPT account in this terminal with a persistent, private `CODEX_HOME` per account. Your shell environment and default `auth.json` stay unchanged. Different accounts can run at the same time. Selecting the actual default login runs plain Codex with an isolation notice; `--require-session` refuses that shortcut. With no identifier, the current default must be a managed account. Disabled accounts cannot launch.

```bash
ccswap codex run 2
ccswap codex run user@example.com --require-session
ccswap codex run                            # actual managed default login
ccswap codex run 2 --shared                  # explicitly join an existing session
ccswap codex run 2 -- exec --json "Review this change"
ccswap codex run 2 -- resume --last
```

Wrapper flags (`--require-session`, `--shared`) must precede `--`. Everything after the first `--` goes to Codex unchanged, including option values that start with `-`. There is no wrapper `--json`, `--strategy`, mapping or recovery flag. Interactive, `resume`, `fork`, `exec` and `exec resume` are supported; remote, login/logout, app-server, cloud and daemon/service commands are refused. Interactive/resume/fork launches disable daemon startup. Without `--cd`, `resume` and `fork` run in the current directory, the one whose configuration was checked, rather than offering the session's recorded directory. A nonempty inherited `CODEX_HOME` is refused before account discovery.

One live session per account is the default. `--shared` joins the existing home without re-seeding its login: **refresh tokens may race, just as with several Codex processes in `~/.codex`**. Capture remains pending until all writers exit. Ctrl-C goes to Codex directly; on POSIX the supervisor forwards SIGTERM/SIGHUP.

### Requirements and policy refusals

Isolated session mode requires **stable Codex 0.164.0 or newer** and checks its version and `--no-daemon` capability without account credentials. This is a minimum and capability check, not a claim that every future release has been tested. The plain default-login shortcut skips the session probe and minimum version check; isolated macOS runs report the platform refusal before resolving or probing the binary. The executable is found on `PATH`. On Windows, ccswap requires native `codex.exe`: it can locate the native binary associated with a standard npm installation, but never executes `.cmd`/`.bat`/PowerShell shims with your arguments. If resolution fails, put native `codex.exe` on `PATH`.

Isolated sessions accept known, supported ChatGPT plans. Workspace-managed business/enterprise/education plans and unknown or missing plans are not supported yet; API-key and other authentication modes are also refused. Local system, user and project configuration is checked conservatively before a writer starts. Unreadable configuration, incompatible login/workspace policy, unsupported profiles, custom account routing, managed requirements that replace the backend, provider or credential store, or managed storage paths outside the session home cause a refusal rather than a policy bypass. A hard-linked session `auth.json` is refused, and on POSIX a reused session home is made private again (`0700`/`0600`). Account-routing environment overrides are removed only from the isolated child, with variable names reported on stderr, never their values.

**macOS isolated runs are currently refused:** `macOS session supervision awaits native CI validation.` The process backend is implemented and tested with mocks, but has no native CI validation yet. The default-login shortcut remains plain Codex.

### Shared definitions and private state

Present `config.toml`, `AGENTS.md`, `AGENTS.override.md`, `rules/` and `prompts/` are shared from the default Codex home. Each user-managed entry in `skills/` is shared separately; the directory itself and `skills/.system/` remain private. POSIX uses symlinks, so definition edits write through to the default home. On Windows, unavailable symlinks fall back to private copies **refreshed on every launch, including shared joins**; edits to those copies are overwritten.

Authentication and credential stores, MCP credentials/locks, rollouts (`sessions/`, `archived_sessions/`), history, thread locks, databases and sidecars, logs, memories, caches, daemon state and plugins remain private. Plugins and history sharing are deferred. MCP logins are per session home: expect a one-time re-authentication there; existing MCP tokens are never copied.

### Capture pending and JSON

Once the whole session home is proven idle, ccswap reconciles persisted credential rotations with the saved login. An initial child exiting is insufficient if descendants or another shared run can still write. **Capture pending** means that adoption is deferred; known live remaining writers preserve the child's exit code. Unknown liveness blocks credential writes and returns wrapper failure, retaining evidence. Conflicts preserve the files for explicit recovery.

`--worktree` and relative `sqlite_home`/`log_dir` overrides for resume/fork are unsupported syntax.

All run-wrapper notices go to stderr, leaving Codex stdout (including `exec --json`) untouched. Exit codes are `2` for parser/unsupported syntax errors, `1` for setup, policy, liveness or capture failures, otherwise Codex's exit code. POSIX signal death maps to `128 + signal`; Windows preserves the native nonnegative status.

`ccswap codex list --json` adds `session` to each account row. `ccswap codex status --json` adds `sessions: [{"number": N, "session": ...}]` for every saved account, including those without a home. A session has `state` (`absent`, `idle`, `live`, `uncertain`, `stale`, `conflict`), `capturePending`, `runCount` and `shared`. The actual default remains active; an isolated session does not become the default. See [JSON output](json.md#codex-session-fields).

When `codex/sessions/` is absent, status, plan labels and usage snapshots do not wait for the account-store lock. Access errors or malformed paths do not count as absence. Credential writers and refreshes still recheck custody under the lock. If the default `auth.json` is temporarily unreadable, backup credentials remain available for read-only quota requests; no refresh is authorized until the default login can be checked again.

### Resolving a session conflict

If both the session home and saved login moved, or their identities drifted, ccswap blocks switch/add/remove/run for that account by design. First make sure no Codex process uses that session home. Then move `<backup root>/codex/sessions/<N>-<email-slug>/` and, if present, its sibling `.<N>-<email-slug>.stale.json` (an interrupted credential update) **out of the `sessions/` directory**, keeping them if you want their history and evidence. Run `codex login` for a fresh login, then `ccswap codex add`, or remove the saved slot with `ccswap codex remove N`. Do not move a live or uncertain home to bypass its guard. Finished run records under the home's `runs/` are kept; a damaged record keeps the account uncertain until it is removed.

The backup root is `~/.local/share/claude-swap/` on Linux/WSL, or `<XDG_DATA_HOME>/claude-swap/` when `XDG_DATA_HOME` resolves to an absolute path (a leading `~` is expanded; empty/relative values use the default). Windows and macOS use `~/.claude-swap-backup/`. The folder suffix is the normalized email slug, so use the existing folder name rather than guessing it.

### Remaining risks

A foreign writer launched manually with `CODEX_HOME=<session home>` is outside the safety model. Process scans guard against observed writers but cannot promise safe token rotation against arbitrary external tools. If such a writer rotates that home while ccswap refreshes the inactive account, the refreshed login is kept in the saved backup and the account reports a conflict for explicit resolution. Shared config can change after preflight, and a remote token rotation that Codex never durably saved before a crash cannot be recovered. Likewise, if neither the session home nor the saved backup can be written after ccswap refreshed an inactive login (for example, a full disk), the new token is lost. The custody checks protect against accidents and crashes, not a hostile process running as the same user.


Use `ccswap codex redeem-reset NUM|EMAIL` to explicitly spend one banked reset for any saved ChatGPT account; this never happens automatically. The command selects the earliest-expiring available card and shows its expiry in the confirmation (`--yes` skips it and is required with `--json`). It sends exactly one redemption request for that selected card, never retries the POST, and re-reads usage afterward. If card details cannot be read, it refuses to spend an unspecified card. `reset` means accepted, with confirmation reported separately; `nothing_to_reset` keeps the card, `no_credit` means none is available, and `already_redeemed` means the card was already used. Check uncertain outcomes with `ccswap codex usage` before retrying.

When session mode is present, an explicit reset uses the selected default/backup access token and never rotates credentials in the POST. A still-valid backup token can redeem while that account has a live session. A 401 can refresh only through the guarded inactive-account path; live, uncertain or conflicting sessions refuse that refresh. The final POST rechecks account identity, session custody and credential consistency under the account-store lock. Uncertain/conflicted state or a pending credential transaction cancels redemption. An unreadable default login prevents refresh reconciliation before it can write.
