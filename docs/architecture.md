# Multi-vendor architecture

Claude Code, Codex and Antigravity keep their purpose-built switchers (`switcher.py`,
`codex.py`, `agy.py`): their semantics (OAuth snapshots, shared quota windows, auto-switch
engines) are rich and tested, and rewriting them would add risk for no gain. Muse, OpenCode
and Kiro share nothing with them but the *shape* of the problem, so they live in a new,
small package, `claude_swap/vendors/`, that reuses the project's infrastructure
(`paths`, `FileLock`, `atomic_write_text`, `spawn`, `error_envelope`).

## Concepts that are kept apart

| Concept | Where |
|---|---|
| Tool identity (Muse, OpenCode, Kiro) | `ToolAdapter.key`, `ToolRegistry` |
| Account identity (a registered account/profile of one tool) | `AccountRecord` (persisted, secret-free), `AccountView` (record + live state) |
| Model provider (anthropic, openai, ...) | `AccountRecord.provider`, only OpenCode fills it |
| Authentication capability | `RegistrationCapability`, `SwitchingCapability`, `auth_summary()` |
| Usage capability | `UsageCapability` returning `UsageResult` with a `UsageKind` and `authoritative` flag |
| Execution capability | `SessionCapability` returning a `LaunchSpec` |
| Capability descriptor | `CapabilityStatus(support, mechanism)` per `Capability` |

```
cli.py / tui/vendors.py / menubar.py / Raycast
              |  (all call the same operations)
        VendorService  -- selection, locking, caching, JSON shaping, no per-vendor branches
              |
   ToolRegistry -> ToolAdapter (+ optional capability protocols)   VendorStore (metadata, secrets)
              |
          Runner (process boundary)  <- SubprocessRunner | FakeRunner in tests
```

Adapters implement only the protocols the vendor really supports (`runtime_checkable`
protocols; `VendorService._need` turns a missing one into `UnsupportedCapability`, exit
status 3). There is no giant interface with `NotImplemented` stubs.

## Usage model

`UsageResult.state` is `available | unavailable | unsupported`. An observation carries its
`kind` (subscription allowance, rate-limit window, credits, local tokens, local cost
estimate), its `source`, `authoritative` (vendor-reported vs locally derived), `scope`,
`fetchedAt` and `cached`. Nothing is inferred: unknown stays unknown, there is no
percentage field, and different kinds are never compared. Results are cached in the service
(60 s success, 15 s failure) and every vendor call has a bounded timeout.

## Auto-switching

Auto-switching needs (a) a quota that can be compared across accounts and (b) a switch that
cannot disrupt running sessions. None of the three tools offers both, so
`VendorService.auto_switch_state` reports `available: false` with the reason for each, and
the existing Claude/Codex auto-switch engines are unchanged. Missing quota is never read as
"unlimited".

## Switching, transactionally

`VendorService.switch`: validate target and capability -> resolve secret -> take the
per-tool `switch-<tool>.lock` -> adapter performs the vendor-supported action -> adapter
verifies where the vendor allows (OpenCode re-lists, Kiro runs `whoami`) -> record the
local "last activated" hint -> release lock. Vendor-controlled state cannot be rolled back by
ccswap; OpenCode is restored through the vendor's own command when a switch does not stick,
and every result says honestly whether it was `verified`.

## Security model

- API keys travel only by stdin or a child's environment, never argv, logs, or JSON.
- ccswap-owned keys live in `<backup-root>/vendors/secrets/<random>.secret` (0600) inside a
  0700 directory; the metadata file never contains a secret. File names are random, aliases
  are validated (`^[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}$`), symlinked stores/secrets are
  refused, loosened modes are repaired, tampered `secretRef` values are ignored.
  Limitation: this is a permission-protected file, not an OS keychain (a keychain write
  needs the secret on a command line); anyone running as you can read it, as they can read
  Muse's own `auth.json`.
- Vendor output is redacted (`redact`) before it reaches a message; unexpected exceptions
  are not swallowed.
- Selectors passed to vendor CLIs are validated, and processes are always started from an
  argv list, never a shell.
- Removing a registration deletes only ccswap's records. Vendor logout is a separate,
  explicit command.
- `vendors.json` is schema-versioned (`schemaVersion: 1`), namespaced per tool, written
  atomically under a file lock, preserves unknown fields and unreadable entries, and is
  never overwritten when corrupt. Nothing in the existing `sequence.json`/credential layout
  changed, so existing installs migrate trivially: there is nothing to migrate. To roll back,
  delete `<backup-root>/vendors/` (your Claude/Codex/Antigravity data is unaffected).

## Adding another vendor

1. Create `vendors/<name>.py` with `class FooAdapter(ToolAdapter)`: set `key`, `label`,
   `binary`, `kinds`; implement `capabilities(info)` (every `Capability`, with the real
   mechanism or why it is unsupported) and `auth_summary()`.
2. Implement only the protocols the product supports: `register`, `observe`, `switch`,
   `usage`, `launch_spec`. Use `self.runner.run(...)` for every process, `info.exe` for the
   binary, `redact`/`short_detail` for vendor text, and return `UsageResult(UNSUPPORTED, ...)`
   rather than guessing.
3. Add the class to `DEFAULT_FACTORIES` in `vendors/registry.py` and the tool name to
   `TOOLS` / the dispatch tuple in `cli.py`.
4. Tests: drive the adapter with `tests/vendor_helpers.FakeRunner`; copy the patterns in
   `tests/test_vendors_muse.py` (secrets never in argv/output, unsupported paths, malformed
   output, timeouts).
5. Document the evidence in `docs/vendor-compatibility.md`.

No Claude, Codex, Antigravity or other-vendor code needs to change.
