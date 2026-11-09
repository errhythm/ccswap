# Kiro CLI

Manage **Kiro CLI** (`kiro-cli`) logins. Kiro IDE is a different product and is not touched.
Kiro agents, steering files, MCP servers, skills and sessions are never modified; an agent is
not an account, and an AWS CLI profile is not a Kiro login. Evidence:
[vendor-compatibility.md](vendor-compatibility.md#kiro-cli).

## Two kinds of account

| Kind | What ccswap stores | Can be |
|---|---|---|
| `login` (Google, GitHub, Builder ID, Identity Center / external IdP) | identity metadata from `kiro-cli whoami` and the login flags; **no tokens** | switched (interactively) |
| `api-key` (`KIRO_API_KEY`, headless) | the key in ccswap's 0600 store | run in isolation |

## Commands

```sh
kiro-cli login                            # log in normally, then register it
ccswap kiro add work                      # captures the current login (whoami); IdC URL/region inferred
ccswap kiro add me --social google        # remember how to log back in
ccswap kiro add ci --api-key              # KIRO_API_KEY via stdin / hidden prompt
ccswap kiro list [--json]                 # * marks the login whoami reports; matched by email + start URL
ccswap kiro status [--json]
ccswap kiro switch me --login --yes       # kiro-cli logout, then kiro-cli login <flags>, then verify
ccswap kiro run ci -- --no-interactive "fix the build"   # kiro-cli chat with KIRO_API_KEY for that child only
ccswap kiro usage                         # unsupported programmatically; run `kiro-cli chat` and type /usage
ccswap kiro remove work                   # forget the registration; Kiro untouched
```

## Why switching is guarded

`kiro-cli logout` ends the whole user login and Kiro offers no non-interactive way back.
A login switch therefore:

1. needs `--login` (a real terminal) and `--yes`;
2. refuses when the *current* login is not registered (you could not return to it);
3. hands the terminal to Kiro for logout and login;
4. re-reads `whoami`: wrong account, or a cancelled login, is reported as failure with the
   exact `kiro-cli login ...` recovery command. It is never reported as success.

Running `kiro-cli chat` sessions keep their old login until restarted. `KIRO_API_KEY` in the
environment overrides any login, and ccswap warns when it is set.

Unlike the older `kiro-swap` prototype, ccswap does not copy Kiro's keychain items or edit
`data.sqlite3`; those are opaque and unsupported.
