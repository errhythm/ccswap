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
- Usage comes from `muse usage --json`, which runs no model turn: your tier, the 5-hour
  window and the weekly window, each with its used % and reset time. Muse reports
  percentages only, so ccswap never shows a made-up "N of M". Muse reports no allowance for
  any API-key sign-in (`api_key`), even for the Muse Code subscription key that the CLI login
  mints, and a new window can be empty (`server_sent_no_usage`); both are shown as
  *unavailable* with the reason, never as 0%. Whether a key is the subscription key or a
  pay-as-you-go key is shown only on dev.meta.ai/api-keys.
- API-key accounts are measured **one by one** (`META_API_KEY` set for that child only).
  Browser logins cannot be: Muse stores one login in the macOS Keychain (so even a
  different `HOME` sees it) and never says whose it is. The tool-level reading is therefore
  labelled "active credential", and browser aliases show no numbers of their own.

## Switching browser logins

ccswap switches Muse browser logins itself, the way it switches Claude Code accounts. It
keeps each registered login in its 0600 store (captured by `ccswap muse add <alias>
--browser`, or imported from a saved file with `--from-file`), and a switch replaces
Muse's `auth.json`:

1. The live login is saved back to its own account first, because Muse refreshes tokens
   in place.
2. ccswap refuses if the live login isn't registered, so it is never lost.
3. The file is written atomically with mode 0600.
4. ccswap accepts the switch when `muse usage` does not say "not logged in".
   A stored login that is only a keychain pointer is refused, and a pointer is never
   saved over a copy that still holds a token. If the check fails, the previous login
   is restored.

Running Muse sessions keep the old login until restarted. The menu bar and the TUI
switch browser logins on this path. An API key is not a menu row and is not a switch.
`ccswap muse run <alias>` sets `META_API_KEY` for that child only.

## Commands

```sh
ccswap muse status                       # install, version, auth state, capability table
ccswap muse add work --api-key-stdin     # key from stdin (or a hidden prompt on a TTY)
ccswap muse add me --browser             # register the current browser login as an alias
ccswap muse list [--json]
ccswap muse run work -- exec "run tests" # isolated: META_API_KEY is set for this child only
ccswap muse switch work --yes            # stores the key; replaces Muse's credential, including a browser login
ccswap muse switch me                    # browser login ccswap has a token for
ccswap muse usage [work] [--refresh] [--json]
ccswap muse remove work                  # forget the registration + ccswap's copy of the key; Muse untouched
```

`active` is always *unknown* for Muse in `list --json` (`null`): Muse cannot say which
credential it will use. After a `switch`, the list only notes "last switched to by ccswap;
unconfirmed". `usage` for a browser alias is unsupported (the numbers would belong to
whichever session is current); `ccswap muse usage` with no alias reports the active
credential and labels it `active-credential`.

Prefer `run` over `switch`: it changes nothing global, so it is safe next to other running
sessions. `switch` is for making a key the machine-wide default; it cannot confirm the
result (Muse cannot be asked which key is stored), and says so (`"verified": false`).

Keys are never accepted as arguments (`--api-key KEY` is a usage error), never printed, and
never logged. See [Security](architecture.md#security-model).
