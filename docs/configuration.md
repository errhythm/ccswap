# Configuration, backup and sharing

## Configuration

Tool preferences live in `settings.json` in the backup root; `ccswap config` reads and edits it with validation, so you never have to find the file or guess valid ranges.

```bash
ccswap config                              # list effective settings ("(default)" = not set)
ccswap config get autoswitch.threshold
ccswap config set autoswitch.threshold 80  # validated: rejects out-of-range values loudly
ccswap config set ui.language de            # en/de/el; restart the dashboard
ccswap config set ui.view codex             # combined (default), claude, or codex
ccswap config set ui.mask on                # hide emails and org names in the TUI, mod and statusline
ccswap config set ui.starPrompt off          # stop asking for a GitHub star after a switch
ccswap config set credentials.backend file  # macOS headless/SSH: never use the Keychain
ccswap config set updates.check off         # stop the daily PyPI check for a newer release
ccswap config set run.shareHistory on       # make --share-history the default for ccswap run
ccswap config set autoswitch.model Fable   # per-model switching (see "auto"); Fable,Opus for several
ccswap config set autoswitch.threshold5h 85 # 5h gate; unset inherits autoswitch.threshold
ccswap config set autoswitch.threshold7d 70 # weekly gate, independent of the 5h one
ccswap config set autoswitch.modelThresholds Fable=40  # Fable gate; Fable=40,Opus=60 for several
ccswap config set autoswitch.weeklyThreshold 97  # 7d/model gate when threshold7d/modelThresholds are unset
ccswap config set autoswitch.windows 5h    # switch on the session window only; 7d for weekly-only, both to reset
ccswap config set autoswitch.warmupFiveHour true  # opt-in Claude warming; uses quota
ccswap config unset autoswitch.threshold   # back to the default
ccswap config path                         # where settings.json lives
```

`ccswap config --help` lists every key with its valid range and default. Hand-editing the file still works — `ccswap config` is just a safer front door. `list` and `get` take `--json` for scripting.

## Backup and migration

Move account data between machines or back it up:

```bash
ccswap export backup.cswap                    # All accounts to a file
ccswap export backup.cswap --account 2        # One account
ccswap export backup.cswap --full             # Include full ~/.claude.json and credential object (same-PC backup)
ccswap import backup.cswap                    # Skips accounts that already exist
ccswap import backup.cswap --force            # Overwrite existing
```

The export file is plaintext JSON and, by default, carries only each account's own login — machine-shared MCP/plugin OAuth tokens and the device token stay on the source machine (`--full` keeps everything, for same-PC backups). If you need encryption, pipe through your tool of choice (e.g. `ccswap export - | gpg -c > backup.gpg`).

If an imported account is the one you're currently logged in as, activate the imported credentials with `ccswap switch N --force` (a plain `switch` to the current account is a safe no-op and won't touch the import).

## Share usage readings between machines

Machines that hold the same accounts can end up spending one usage-endpoint budget: when they share a login (moved between them with `export`/`import`, so the same token is live on each), or when the account's usage requests are limited per account rather than per token. Their polling then adds up. `import-usage` lets one machine poll and hand its readings to the others:

```bash
ccswap list --json | ssh laptop ccswap import-usage - --hold 600
```

### How it works: matching, holds & when a hold ends

The input is `ccswap list --json` output (merged or `--provider claude`). Each row with `usageStatus: "ok"` is matched to a local account by email and organization, and adopted when it is newer than the reading already stored. Its age comes from `usageAgeSeconds`, so the two machines' clocks never have to agree; a script that delays the hand-over should add the delay to that field. `--hold SECONDS` keeps every collector on the receiving machine (`list`, `status`, `auto`, the dashboard, the menu bar) from fetching those accounts for that long, and the held reading stays trusted for switch decisions meanwhile. A hold never runs past the reading's earliest window reset (per-model windows included), nor past an hour after the reading was taken. Renew it with each hand-over, or lift it early with `--hold 0`; when it lapses, the machine goes back to fetching for itself.
