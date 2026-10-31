# Commands

## Switch accounts

Rotate to the next account:

```bash
ccswap switch
```

Or switch to a specific account:

```bash
ccswap switch 2
ccswap switch user@example.com
ccswap switch dev                # or by alias, once set with `ccswap alias 2 dev`
```

Not sure which one? `ccswap list` is the dashboard — every account's quota usage and reset times at a glance:

```bash
ccswap list
```

Or ccswap auto-picks by remaining quota — `ccswap switch --strategy best` (most quota left), `--strategy next-available` (skip rate-limited accounts), or `--strategy consume-first` (the account with quota left whose weekly limit resets soonest, so none of it expires unused; falls back to `best` when no reset time is known).

**Note:** You usually don't need to restart — on Linux/Windows the new account is picked up automatically, and on macOS after the Keychain cache expires. To apply it instantly, restart Claude Code or reopen the VS Code extension tab. See [Restarts and session behavior](how-it-works.md#restarts-and-session-behavior) for the per-platform details.

## Refresh expired tokens

If an account's token expires, log back into Claude Code with that account and re-run:

```bash
ccswap add
```

This will update the stored credentials without creating a duplicate.

## Add an account from a raw token or API key

If you only have a long-lived setup-token (e.g., produced by `claude setup-token`)
or a Console API key (`sk-ant-api03-...` workspace key, `sk-ant-usr-...` personal
key, or a service-account key) and you don't want to log in via the browser
flow first — useful on headless servers or when receiving a token from another
machine — register it directly. The token type is auto-detected:

```bash
ccswap add-token sk-ant-oat01-...             # OAuth setup-token
ccswap add-token sk-ant-api03-...             # API key (any Console key family)
ccswap add-token sk-ant-oat01-... --slot 3
ccswap add-token - --slot 3                   # read token from stdin
ccswap add-token --email user@example.com     # optional label override
```

`--email` is optional; omitted values use `setup-token-{slot}@token.local`
(or `api-key-{slot}@token.local` for API keys). No Anthropic API calls are made.

**API-key accounts.** Any bare `sk-ant-...` value that is not an OAuth token
(`sk-ant-oat...`) registers a managed API-key account (the kind Claude Code uses
after `/login` with a key) rather than an OAuth setup-token. It switches like any
other account; since API keys have no subscription quota, they show no usage and the
usage-aware `switch` strategies never skip them as rate-limited. Create personal and
service-account keys scoped to a single workspace: a multi-workspace key needs an
`anthropic-workspace-id` header on every request, which Claude Code does not add for
a stored key.

## Command reference

```bash
ccswap run 2                     # Run an account in this terminal only (session mode)
ccswap auto                      # Auto-switch when nearing rate limits (see [auto-switching.md](auto-switching.md))
ccswap auto --install-service    # Keep auto-switch running via launchd (macOS)
ccswap codex list                # List saved Codex CLI accounts
ccswap codex switch 2            # Switch the file-backed Codex login
ccswap config                    # Show or edit settings (see [configuration.md](configuration.md))
ccswap list                      # Claude + Codex accounts with quota and reset times
ccswap list --provider codex     # Restrict to one provider (claude|codex|all)
ccswap list --token-status       # Add source-labelled OAuth token diagnostics
ccswap status                    # Show current account
ccswap add --slot 3              # Add account to a specific slot (prompts before overwrite)
ccswap add --alias dev           # Add account and give it a short alias
ccswap remove 2                  # Remove an account
ccswap disable 2                 # Hold an account out of auto-rotation (keeps its login)
ccswap enable 2                  # Return a disabled account to rotation
ccswap alias 2 dev               # Give an account a short alias (usable anywhere NUM|EMAIL is)
ccswap alias 2 --unset           # Remove an account's alias
ccswap alias                     # List all aliases
ccswap move 2 1                  # Assign an account to a slot (relocates to an empty slot, swaps if taken)
ccswap unclaimed                 # List stashed credential entries (slot + why they were stashed)
ccswap unclaimed --purge ID      # Drop one (deletes its bytes; recover with /login + `ccswap add`)
ccswap tui                       # Interactive dashboard (also: bare `ccswap`)
ccswap watch                     # Dashboard, opened on the live watch page
ccswap upgrade                   # Upgrade ccswap to the latest version
ccswap purge                     # Remove all ccswap data
```

`ccswap codex redeem-reset NUM|EMAIL [--yes] [--json]` explicitly redeems one
banked reset for a saved ChatGPT account (see [Codex](codex.md)).
