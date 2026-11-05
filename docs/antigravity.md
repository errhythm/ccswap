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

The supported native store is macOS Keychain. An SSH environment explicitly selects Antigravity's file backend. Other native keyrings, config-root overrides, ADC, Gemini API-key mode and enterprise auth contexts are rejected. A conflicting fallback file or keyring-unavailable marker also blocks shared switching. Empty inventories remain safe to list on other platforms.

ccswap keeps full credential wrappers in private files under its Antigravity account directory. It saves the outgoing login, including an account not yet managed, before changing the native login. It checks for external changes before writing and verifies the result. Antigravity itself does not take ccswap's lock, so a residual race with an external writer remains. Keychain writes that would expose credentials through process arguments are refused.

If a usage refresh conflicts with a changed saved credential, ccswap preserves the refreshed wrapper in a private `refresh-conflict-*.json` file in its Antigravity account directory. If that directory cannot accept the refreshed wrapper, ccswap retains the private temporary usage directory instead. The error reports the recovery file's absolute path. Keep that file private and stop other Antigravity sessions before recovery. Do not overwrite a newer saved login with it. Signing in again and running `ccswap agy add` replaces the saved login through the normal capture flow; retain the recovery file until the account works again.

Source and verification note, 2026-11-05. This integration targets Antigravity CLI 1.5.0 and the [official CLI installation and authentication documentation](https://antigravity.google/docs/install?tab=cli). Native `/usage` output was verified. Capture, switching, refresh persistence, CLI JSON and terminal interaction are covered with synthetic accounts. Live switching between two Google accounts remains unverified because this host blocks live credential reads.

To reproduce the synthetic checks and terminal screenshot from a repository checkout, run:

```sh
uv run pytest tests/test_agy.py -n 4 --basetemp=/tmp/ccswap-agy-proof
```

The terminal test writes `agy-switch.svg` beneath its test directory in `/tmp/ccswap-agy-proof`.
