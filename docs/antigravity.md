---
summary: Save personal Antigravity accounts, inspect scoped quotas, and switch the shared login.
read_when:
  - Managing Antigravity accounts through ccswap or its terminal dashboard
  - Checking Antigravity storage support and shared-login limitations
---

# Antigravity accounts

Sign in with `agy`, then save the current personal Google account. Sign in with the next account and save it again.

```sh
ccswap agy add
ccswap agy list
ccswap agy status --json
ccswap agy usage --json
ccswap agy switch 2
ccswap agy remove 2 --yes
ccswap list --provider agy --json
```

`ccswap list --json` includes an `agy` section alongside `claude` and `codex`. The terminal dashboard supports Antigravity views, adding the current login, manual switching and removal. Antigravity has no automatic switching, best-account strategy, disable control or isolated `run` command.

Switching changes the shared native Antigravity login. Restart existing CLI, App or IDE sessions to use it. Removing a saved account removes only its ccswap backup and quota cache. It leaves the native login signed in.

Usage runs only the native `/usage` command. Each Gemini, Claude/GPT or Argon quota bucket keeps its own group, window and reset time. No account-wide quota is inferred. Reads are cached for 60 seconds. A failed request keeps the last successful measurements.

The Antigravity App alone is not sufficient for usage reads. Native `/usage` requires the separate [Antigravity CLI installation](https://antigravity.google/docs/getting-started/).

Each snapshot has one 45-second subprocess refresh budget after it acquires the Antigravity custody lock. Each child receives the time remaining after temporary storage setup and recovery journal writes. Accounts with the oldest persisted attempt run first, with never-attempted accounts first and account number breaking ties. Accounts skipped when the budget runs out keep their previous attempt and error state. Keychain operations have separate timeouts, so this is a bounded subprocess budget, not a 45-second limit on total lock time.

The supported native store is macOS Keychain. An SSH environment explicitly selects Antigravity's file backend. Other native keyrings, config-root overrides, ADC, Gemini API-key mode and enterprise auth contexts are rejected. A conflicting fallback file or keyring-unavailable marker also blocks shared switching. Empty inventories remain safe to list on other platforms.

ccswap keeps full credential wrappers in private files under its Antigravity account directory. It saves the outgoing login, including an account not yet managed, before changing the native login. It checks for external changes before writing and verifies the result. Antigravity itself does not take ccswap's lock, so a residual race with an external writer remains. Keychain writes that would expose credentials through process arguments are refused.

Before a usage child starts, ccswap journals its private recovery file in the quota cache. It refuses to start if that journal cannot be saved. If a usage refresh conflicts with a changed saved credential, ccswap preserves the refreshed wrapper in a private `refresh-conflict-*.json` file in its Antigravity account directory. If that directory cannot accept the refreshed wrapper or update the journal, ccswap retains the private temporary usage directory instead. The quota cache keeps the recovery kind and absolute file path, including after an interrupted process. Successful refreshes save the credentials and clear the journal before deleting temporary state. The CLI and full and compact cards show the recovery instruction even with cached quota. Pending recovery blocks further quota refreshes, native-to-saved synchronization and switching to that account, including when its saved file is missing or malformed. Keep the recovery file private and stop other Antigravity sessions before recovery. Do not overwrite a newer saved login with it. Sign in again and run `ccswap agy add` to clear recovery for that identity after a successful capture. Incidental outgoing captures do not clear it. Retain the recovery file until the account works again.

Source and verification note, 2026-11-05. This integration targets Antigravity CLI 1.5.0 and the [official CLI installation and authentication documentation](https://antigravity.google/docs/install?tab=cli). Native `/usage` output was verified. Capture, switching, refresh persistence, CLI JSON and terminal interaction are covered with synthetic accounts. Live switching between two Google accounts remains unverified because this host blocks live credential reads.

To reproduce the synthetic checks and terminal screenshot from a repository checkout, run:

```sh
uv run pytest tests/test_agy.py -n 4 --basetemp=/tmp/ccswap-agy-proof
```

The terminal test writes `agy-switch.svg` beneath its test directory in `/tmp/ccswap-agy-proof`.
