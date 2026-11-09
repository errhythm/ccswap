# Muse Code

Manage Meta's **Muse Code CLI** (`muse`), not the Meta API as a model provider. Capability
details and evidence: [vendor-compatibility.md](vendor-compatibility.md#muse-code).

## What it can and cannot do

- Muse has no `whoami` and no way to choose between browser sessions. ccswap therefore keeps
  *local aliases*: an **API-key** account (ccswap stores the key) or a **browser** account
  (alias only, no token copied).
- Credential precedence in Muse is `META_API_KEY` > stored key > stored browser session. If
  `META_API_KEY` is exported, nothing ccswap stores can take effect in that shell. ccswap
  warns about it.
- Usage is whatever `muse usage --json` reports. It runs no model turn. It is usually empty
  for pay-as-you-go keys, and ccswap then prints the vendor's reason instead of a number.

## Commands

```sh
ccswap muse status                       # install, version, auth state, capability table
ccswap muse add work --api-key-stdin     # key from stdin (or a hidden prompt on a TTY)
ccswap muse add me --browser             # register the current browser login as an alias
ccswap muse list [--json]
ccswap muse run work -- exec "run tests" # isolated: META_API_KEY is set for this child only
ccswap muse switch work --yes            # `muse auth set --api-key-stdin`; replaces Muse's stored credential
ccswap muse switch me --login            # interactive `muse login` (browser accounts cannot be switched)
ccswap muse usage [work] [--refresh] [--json]
ccswap muse remove work                  # forget the registration + ccswap's copy of the key; Muse untouched
```

Prefer `run` over `switch`: it changes nothing global, so it is safe next to other running
sessions. `switch` is for making a key the machine-wide default; it cannot confirm the
result (Muse cannot be asked which key is stored), and says so (`"verified": false`).

Keys are never accepted as arguments (`--api-key KEY` is a usage error), never printed, and
never logged. See [Security](architecture.md#security-model).
