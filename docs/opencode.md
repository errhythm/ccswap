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
ccswap opencode usage                     # explains that no provider quota exists
ccswap opencode usage --local             # `opencode stats`: local, all providers, NOT a quota
```

There is no `opencode run <provider> <account>`: OpenCode resolves credentials in a shared
server, so per-process account isolation does not exist and ccswap does not pretend it does.
Running servers keep the previous account until restarted.

Selectors become argv entries; anything that is not `[A-Za-z0-9._@+:/-]` (so no leading
`-`) is rejected before any process starts.
