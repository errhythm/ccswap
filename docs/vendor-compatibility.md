# Vendor compatibility: Muse Code, OpenCode, Kiro CLI

This matrix says what ccswap can really do with each tool, and how. It was written
against the binaries installed while developing the integration:

| Tool | Version checked | How it was verified |
|---|---|---|
| Muse Code (`muse`) | 1.4.14 (1.4.14-R7309.1) | `--help`, `auth set --help`, `login/logout/usage --help`, a live `muse usage --json`, [auth docs](https://www.musemuse.dev/docs/muse-code/auth), [subscription docs](https://www.musemuse.dev/docs/muse-code/subscriptions) |
| OpenCode (`opencode`) | 1.18.35 | `--help`, `providers/auth` subcommands, live `auth list`, live `stats`, the shape (keys only) of `auth.json`, [providers docs](https://opencode.ai/docs/cli/providers) (describes a *newer* release) |
| Kiro CLI (`kiro-cli`) | 3.1.0 | `--help`, `login/logout/whoami/profile/chat --help`, live `whoami -f json`, live `chat --list-models`, Kiro headless-mode documentation |

Legend: **supported** = implemented and tested against a faithful fake of the real process
boundary, and (where noted) observed live; **conditional** = works only for some account
kinds or tool versions; **unsupported** = the vendor product does not offer it, so ccswap
says so instead of imitating it; **unverified** = depends on behaviour that could not be
checked here.

`ccswap vendors capabilities [muse|opencode|kiro] [--json]` prints the same table from the
code, so the table and the behaviour cannot drift apart.

## Muse Code

| Capability | Status | Mechanism |
|---|---|---|
| Binary / discovery | supported | `muse` on `PATH` |
| OS support | supported | macOS, Linux, Windows follow the vendor; CI runs the suite on all three (Windows skips POSIX-only fake-executable tests) |
| Version | supported | `muse --version` (live: `Muse Code 1.4.14 (...)`) |
| Auth methods | supported | browser `muse login`, `muse auth set --api-key-stdin`, `META_API_KEY` |
| Auth precedence | supported | `META_API_KEY` > stored key > stored browser session (vendor docs) |
| Auth state | conditional | `META_API_KEY` presence and the provider list in `~/.config/muse/auth.json` (`$XDG_CONFIG_HOME/muse`). The credential **type and the account identity are not exposed** by muse |
| Account enumeration / identification | unsupported | no `whoami`/`accounts` command; ccswap only knows accounts you register |
| Registration | supported | API key from stdin/no-echo prompt into ccswap's 0600 store; browser sessions are an alias only, no token is copied |
| Native activation | conditional | API-key accounts: `muse auth set --provider meta --api-key-stdin` (key on stdin). **Replaces** whatever Muse has stored, including a browser session, so `--yes` is required. Browser accounts: not switchable, only `muse login` |
| Post-switch verification | unsupported | muse offers no way to read back the stored identity; the result says `verified: false`, and `active` stays `null` (unknown) in listings |
| Credential ownership | n/a | Muse owns its stored credential; ccswap owns only the API keys you registered with it |
| Global vs project settings | unverified | ccswap never reads or writes `settings.json`/`trust.json`; Muse sandbox and approval modes are untouched |
| Model selection | unsupported | left to `muse --model` / settings; not managed |
| Usage / quota | supported | `muse usage --json` (no model turn). Verified live on 1.4.14: `{"tier", "window": {"usedPercent", "resetsAtMs"}, "weekly": {...}, "asOfMs", "source"}` → plan, a 5-hour window and a weekly window, each with used % and reset time. Muse reports percentages only, so no request or token denominator is shown. `allowance: null` → *unavailable* with the reason (`api_key`: an API-key sign-in, for which Muse reports no allowance even when it is the Muse Code subscription key; `server_sent_no_usage`: nothing yet in this window). Unknown new fields are kept, not dropped |
| Per-account usage | conditional | API-key accounts: measured one by one, with `META_API_KEY` set for that child only (verified live: Muse answers for that key, not the login). Browser logins: Muse keeps **one** login, in the **macOS Keychain** (verified: a throwaway `HOME` still saw it), and never says whose it is. So browser aliases get no numbers, and the tool-level reading is labelled "active credential" |
| Rate-limit detection | unsupported | no stable interface |
| Auto-switching | unsupported | per-account allowances are not comparable, and browser sessions cannot be switched |
| Per-process isolation | conditional | `ccswap muse run <alias>`: child gets `META_API_KEY` (documented top precedence); nothing global changes. Not available for browser sessions |
| Running-session restart | n/a | running Muse sessions keep their credential until restarted (reported) |
| Logout | conditional | `muse logout` (never run implicitly; `ccswap muse remove` only forgets the registration) |
| Failure / recovery | supported | failed `auth set` leaves local metadata untouched; messages are redacted |

## OpenCode

| Capability | Status | Mechanism |
|---|---|---|
| Binary / discovery | supported | `opencode` on `PATH` |
| Version | supported | `opencode --version` (live: `1.18.35`) |
| Capability detection | supported | commands are **probed from `--help`**, not inferred from version numbers: `auth --help` (is there `switch`?), `auth list --help` (is there `--format`?), `auth login --help` (`--provider`?) |
| Enumeration, newer releases | supported | `opencode auth list --format json`. The JSON field names could **not** be verified against a live new release; parsing is defensive (several shapes) and covered by tests, but treat as *unverified live* |
| Enumeration, 1.18.x | conditional | no JSON listing; ccswap reads **provider ids and credential types only** (never values) from the legacy `~/.local/share/opencode/auth.json` (`$XDG_DATA_HOME`), one credential per provider |
| SQLite credential database | n/a | **never read or written** by ccswap |
| Provider vs account | supported | an account is `(provider, account)`; several providers are active at once; never modelled as one global login |
| Registration | supported | an alias for a provider/account OpenCode already lists (verified to exist first) |
| Native activation | conditional | `opencode auth switch <provider> <account>` where the installed OpenCode has it. **1.18.35 does not**, so `switch` exits 3 with the upgrade/`/connect` instructions |
| Post-switch verification | supported | the account list is re-read; if the vendor did not actually change, ccswap switches back through the same command and reports failure |
| Login / logout | supported | `opencode auth login [--provider p]`, `opencode auth logout <provider> [account]` (explicit, `--yes`) |
| Provider configuration | supported | `opencode.json` (`$OPENCODE_CONFIG`, `$XDG_CONFIG_HOME`) read-only: model and configured provider *names*, shown separately from login state; never rewritten |
| Usage / quota | conditional | **Local statistics only.** `opencode db "<constant SELECT>" --format json` (the official CLI's DB tool) aggregates assistant messages per provider and model: message count, input/output/reasoning/cache tokens and OpenCode's own USD cost estimate for 24h/7d/30d/all time. The query reads only the `message` table, never credential/account tables or message content. Older releases fall back to `opencode stats` text. **Provider quotas** (subscription windows, credits, rate limits) are not reported by OpenCode or by any interface it ships, so they are shown as not reported |
| Per-account usage | unsupported | message records carry provider and model but no account, so a provider account gets no numbers rather than the provider's total |
| Rate-limit detection | unsupported | provider-specific; not exposed |
| Auto-switching | unsupported | no verified per-account quota to compare |
| Per-process isolation / `run` | unsupported | credentials are resolved by the shared OpenCode server; there is no per-process account selector, so no `opencode run` command is offered |
| Running-session restart | n/a | servers/sessions keep the old account until restarted (reported) |

## Kiro CLI

Scope: **Kiro CLI only.** Kiro IDE is a different product with no verified shared
account-selection interface; ccswap does not touch it. Kiro agents, steering, MCP servers,
skills and session data are never read or modified; an agent is not an account.

| Capability | Status | Mechanism |
|---|---|---|
| Binary / discovery | supported | `kiro-cli` on `PATH` (the IDE launcher `kiro` is deliberately not accepted) |
| Version | supported | `kiro-cli --version` (live: `kiro-cli 3.1.0`) |
| Auth methods | supported | Google/GitHub (`--social`), AWS Builder ID (`--license free`), IAM Identity Center (`--license pro --identity-provider URL --region R`), `KIRO_API_KEY` headless |
| Auth state / identity | supported | `kiro-cli whoami -f json` -> `accountType`, `email`, `startUrl`, `region` (live). Output may be followed by plain-text profile lines; only the first JSON value is read |
| Account enumeration | conditional | only the single active login is visible; no account list exists |
| Registration | supported | login: identity metadata copied from `whoami` (no tokens, no keychain/SQLite access); api-key: key via stdin/prompt into ccswap's 0600 store |
| Native activation | conditional | login accounts: confirmed `kiro-cli logout` then `kiro-cli login <flags>` with the terminal handed to Kiro (browser/device flow), then verified with `whoami`. Needs `--login` **and** `--yes`, and the *current* login must itself be registered, so a user is never logged out of an account they could not return to. API-key accounts are process-scoped: they are **run**, not switched |
| Opaque stores | n/a | Kiro's keychain items and `data.sqlite3` are **not read, copied or written** (unlike the older `kiro-swap` experiment) |
| Identity Center / enterprise | conditional | works as a login account with its start URL and region; `kiro-cli profile` is interactive and not automated |
| AWS CLI profile | n/a | not equivalent to a Kiro login; ignored |
| Usage / quota | supported | `kiro-cli chat --no-interactive --v2 "/usage"`. Verified on 3.1.0: the V2 harness answers the slash command locally and exits 0. **No credits are consumed:** three back-to-back runs left the credit count unchanged. Output: `Estimated Usage \| resets on 2026-12-01 \| KIRO PRO+` / `Credits (1086.43 of 2000 covered in plan), 54.3%` / `Overages: …` → plan, credits used/limit/remaining, a recomputed percentage, the reset **date** (Kiro gives no time), overage state, and whether an organization manages the plan. The older box layout and bonus credits are parsed too. Without `--v2` a V3 engine may send the text to the model, so a CLI lacking the flag is never probed. Each run saves an empty V2 session, so the probe runs from a fixed ccswap directory (`<backup-root>/vendors/kiro-probe`) to keep those out of project resume lists |
| Per-account usage | conditional | Only the signed-in login can be measured (checked with `whoami` first), plus API-key accounts in isolation (`KIRO_API_KEY` for that child; not verified live without a key). Other registered logins show **their own** last observation, marked stale with its age, or nothing |
| Rejected source | — | Kiro's private `GetUsageLimits` service with the token read from `data.sqlite3` (used by some third-party tools). Rejected: it is undocumented, and it means reading Kiro's own credential store |
| Rate-limit detection | unsupported | no stable interface |
| Auto-switching | unsupported | usage is readable now, but switching a login needs an interactive logout/login |
| Per-process isolation | conditional | API-key accounts: `ccswap kiro run <alias> -- <chat args>` sets `KIRO_API_KEY` for that child only. Login accounts share the one machine-wide login and cannot run side by side |
| Running-session restart | n/a | running `kiro-cli chat` sessions keep the old login until restarted (reported) |
| Failure / recovery | supported | logout failure leaves the login untouched; login failure/cancel prints the exact `kiro-cli login ...` recovery command; wrong account after login is reported, not hidden |

## Surfaces

| Surface | Muse | OpenCode | Kiro |
|---|---|---|---|
| CLI (`ccswap <tool> ...`) | list, status, usage, add, remove, switch, run | list/accounts, status, usage, add, remove, switch, login, logout | list, status, usage, add, remove, switch, run |
| TUI (own page: keys `3` / `5` / `4`, menu entries) | accounts, usage bars, switch (API key, confirmed), remove | provider accounts, local stats, switch, remove | accounts, credits, reset; login switch explained (CLI) |
| Menu bar (macOS) | own submenu: plan, windows, resets, accounts, refresh | own submenu + switch | own submenu: credits, reset, accounts, refresh |
| Raycast | Show Muse Code Accounts | Show OpenCode Accounts (+ switch) | Show Kiro CLI Accounts |
| All tools | `ccswap usage`, TUI overview (`0`), Raycast "Show All Usage" | | |

Menu bar and Raycast only switch OpenCode accounts (and Claude/Codex through their existing
commands), because those switches are non-interactive and need no confirmation. Muse and
Kiro rows show the exact command to run. The menu content is built in `vendors/menu.py`
(unit-tested); `menubar.py` only renders it.
