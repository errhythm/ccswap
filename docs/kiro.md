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
ccswap kiro usage                         # plan, credits used/limit, reset date (runs /usage locally, no credits)
ccswap kiro remove work                   # forget the registration; Kiro untouched
```

## Profiles (several AWS accounts in one organization)

One Identity Center login can see several Kiro profiles, one per AWS account in the
organization, and each has its own subscription and credits. ccswap treats each profile
as a separate account: `kiro-cli whoami` prints the active profile ARN, which becomes part
of the account's identity.

```sh
ccswap kiro add work-a                         # the active profile
ccswap kiro add work-b --profile-arn arn:aws:codewhisperer:us-east-1:<account>:profile/<id>
ccswap kiro switch work-b --login              # opens Kiro's own `kiro-cli profile` picker; no logout
```

The switch is verified with `whoami`. Picking the wrong profile is reported, never
counted as success. `/usage` is filed under whichever profile was active when it was read.

## Usage

`ccswap kiro usage` runs `kiro-cli chat --no-interactive --v2 "/usage"`. The V2 harness
answers the slash command itself, so no model turn runs and no credits are used (verified
on 3.1.0 with repeated runs). ccswap shows the plan, credits used of the plan allowance,
what remains, the reset date, bonus credits when present, and whether overages are on or
the plan is managed by an organization. Kiro reports a reset *date*, not a time, so none is
invented. Each probe leaves an empty V2 session behind; ccswap runs it from its own
`vendors/kiro-probe` directory, so those sessions never show up in your projects.

Only the signed-in login can be measured. ccswap checks `whoami` before and after the probe
and drops the reading if the login changed in between. kiro-cli 3.1 reports a signed-out
state as `{"account": null}`, which is shown as "sign-in required". A registered login that isn't active keeps
showing **its own** last reading, marked stale with its age, and never another account's.
API-key accounts are measured with `KIRO_API_KEY` set for that child only.

## Why switching is guarded

`kiro-cli logout` ends the whole user login and Kiro offers no non-interactive way back.
A login switch therefore:

1. needs `--login` (a real terminal) and `--yes`;
2. refuses when the *current* login is not registered (you could not return to it);
3. hands the terminal to Kiro for logout and login;
4. re-reads `whoami`: wrong account, or a cancelled login, is reported as failure with the
   exact `kiro-cli login ...` recovery command. It is never reported as success.

If `kiro-cli whoami` itself fails or times out, ccswap changes nothing: only an explicit
"not logged in" counts as logged out.

Identity is `email` + Identity Center start URL (+ account type when both sides report it).
`whoami` does not name the social provider, so ccswap refuses to register two logins with the
same identity (for example a Google and a GitHub login with one email) instead of guessing.

Running `kiro-cli chat` sessions keep their old login until restarted. `KIRO_API_KEY` in the
environment overrides any login, and ccswap warns when it is set.

Unlike the older `kiro-swap` prototype, ccswap does not copy Kiro's keychain items or edit
`data.sqlite3`; those are opaque and unsupported.
