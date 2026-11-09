# OpenCode

OpenCode is both a coding agent and a hub for many model providers. An OpenCode account in
ccswap is a **(provider, account)** pair, several providers are active at once, and the
login state is shown separately from `opencode.json` provider configuration. Evidence:
[vendor-compatibility.md](vendor-compatibility.md#opencode).

## Version differences

ccswap probes `opencode auth --help` instead of trusting a version number:

| Installed OpenCode | Listing | Switching |
|---|---|---|
| has `auth switch` (newer) | `opencode auth list --format json` | `opencode auth switch <provider> <account>`, verified by re-listing |
| 1.18.x (no `switch`) | provider ids/types from the legacy `auth.json`, one credential per provider | **unsupported**: exit code 3 with upgrade / `/connect` instructions |

ccswap never reads or writes OpenCode's SQLite credential database and never writes
`auth.json`. All changes go through `opencode auth ...`.

## Commands

```sh
ccswap opencode list                      # alias: accounts. Provider, account, type, * active
ccswap opencode status [--json]           # version, capabilities, configured providers/model
ccswap opencode add work --provider anthropic --account work   # local alias for an existing account
ccswap opencode switch anthropic work     # native switch + verification
ccswap opencode switch openai personal
ccswap opencode login [provider]          # opencode auth login (interactive)
ccswap opencode logout anthropic work --yes   # deletes OpenCode's own credential
ccswap opencode remove work               # forget the alias only
ccswap opencode usage                     # local tokens + cost per provider/model (24h/7d/30d/all)
ccswap opencode usage --local             # raw `opencode stats` text
```

**OpenCode Go is not credits.** Go limits each model separately in dollar-equivalent usage
(token price × tokens): a rolling 5-hour window of 20% and a weekly window of 50% of the
model's monthly limit ([docs](https://opencode.ai/docs/go/)). OpenCode records each
message's cost at Go's token prices, so ccswap shows each Go model's usage for the last 5
hours, 7 days and 30 days from those records. The monthly limit per model depends on your
plan (Go, Go Plus, Teams) and is only shown in the OpenCode console, so no percentage is
invented. When the newest Go request failed (for example `403 Workspace access denied`), the
headline says "last request refused".

Usage is **local statistics**: a constant, read-only `SELECT` through the official
`opencode db` command over the `message` table (never the credential or account tables,
never message content). It gives message counts, token types and OpenCode's own USD cost
estimate per provider and model. These are not provider quotas, and OpenCode exposes none
(subscription windows, credits or rate limits). Messages record provider and model but no
account, so per-account numbers do not exist; a provider account shows none rather than
its provider's total.

There is no `opencode run <provider> <account>`: OpenCode resolves credentials in a shared
server, so per-process account isolation does not exist and ccswap does not pretend it does.
Running servers keep the previous account until restarted.

If several accounts of one provider share a label, `add`/`switch` ask for the account id
instead of picking one. A vendor identifier is never reinterpreted as a list position, and
if a release lists no active flag the switch is reported `verified: false` rather than as
a failure or a success.

Selectors become argv entries; anything that is not `[A-Za-z0-9._@+:/-]` (so no leading
`-`) is rejected before any process starts.
