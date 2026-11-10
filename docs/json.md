# JSON output for scripting

Add `--json` to `list`, `status`, or `switch` to emit a single machine-readable JSON object on stdout (human-readable notices go to stderr). Useful for scripting auto-swap and quota tracking.

```bash
ccswap list --json                   # BOTH providers, merged (default)
ccswap list --provider claude --json # Claude Code accounts only (bare payload)
ccswap list --provider codex --json  # Codex accounts only (bare payload)
ccswap status --json                 # current active account
ccswap switch --strategy best --json # switch, then report the result
ccswap switch 2 --json
```

`ccswap list` now covers both providers by default. In `--json` mode that means the output is keyed by provider (`claude`/`codex`); pass `--provider claude` (or `codex`) to get a single provider's **bare** payload — the same shape older scripts already parse.

## Example output & schema notes

Merged (default) — `ccswap list --json`:

```json
{
  "claude": {
    "schemaVersion": 1,
    "activeAccountNumber": 2,
    "accounts": [
      { "number": 2, "email": "you@example.com", "active": true, "usageStatus": "ok",
        "usage": { "fiveHour": { "pct": 25.0, "resetsAt": "2026-06-22T23:29:59Z" },
                   "sevenDay": { "pct": 16.0, "resetsAt": "2026-06-26T17:59:59Z" } } }
    ]
  },
  "codex": {
    "provider": "codex",
    "activeAccountNumber": 1,
    "accounts": [
      { "number": 1, "email": "you@example.com", "authMode": "chatgpt",
        "planType": "team", "label": "Codex Team", "active": true }
    ]
  }
}
```

Single provider — `ccswap list --provider claude --json` — returns just the inner Claude object above (`schemaVersion`, `activeAccountNumber`, `accounts`), unchanged from before. **Migration:** scripts that parsed the old top-level `accounts` from `ccswap list --json` should switch to `ccswap list --provider claude --json`.

Every Claude payload carries a `schemaVersion` (currently `1`); on a handled error stdout is `{"schemaVersion":1,"error":{...}}` with a non-zero exit code. `--switch`/`--switch-to` report `{"switched": true|false, "from": …, "to": …, "reason": …}`.

A row carries an additive `loginExpiresAt` (ISO-8601 UTC) when the stored login records when its refresh token expires, which is the moment the slot will need a fresh `/login` and `ccswap add --slot N`; a script can warn a few days ahead instead of discovering `relogin_required`. Absent when Claude Code recorded no such date for that login.

Usage is served from a per-account cache: when the usage API is briefly unreachable, the last-known numbers are shown instead of nothing (the human view marks them with their age, e.g. `· 2m ago`). Rows with decision-trusted usage carry additive `usageFetchedAt`/`usageAgeSeconds` fields telling you how old the measurement is. Whenever `usage` is null but a last-known measurement exists — data too old to drive a decision (`usageStatus` stays `unavailable`), or a row in a non-`ok` state such as `token_expired` — additive `lastGoodUsage`/`lastGoodFetchedAt`/`lastGoodAgeSeconds` fields preserve the human display without making the account actionable. When `usage` is null and nothing else explains it (`usageStatus` is `unavailable`), an additive `usageError` names the last fetch failure by kind (e.g. `http-429`, `timeout`) and, while the cache is backing off from it, `usageRetryAt` gives the time of the next attempt. These fields apply to list rows and the managed active row from `status --json`. An account held out of rotation with `ccswap disable` carries an additive `"disabled": true` on its row (absent otherwise).

An account row also carries an additive `alias` field once one is set with `ccswap alias` (e.g. `"alias": "dev"`); accounts without one simply omit the key.

Weekly windows (`sevenDay` and per-model `scoped` entries — never `fiveHour`) additively carry pace fields once the week is ~a day old: `expectedPct` (where usage would sit if spread evenly across the week) and `aheadOfPace` (`true` when meaningfully above that — the same signal the human views show as an `(ahead)`/`(ahead of pace)` marker). `projectedExhaustionAt`/`willLastToReset` extrapolate the current rate into an ETA to 100% and a yes/no "will it last to the reset"; they stay `--json`-only since a linear projection is too rough to present as fact in the UI.

`ccswap auto --json` emits an event *stream* instead — one JSON object per line (`{"schemaVersion":1,"event":"switch","ts":…, …}` with kinds like `poll`, `switch`, `no-switch`, `account-quarantined`, `all-exhausted`, `error`). The contract is additive: new kinds and fields may appear, so scripts should ignore unknown ones.

File-only quota consumers can read [`<backup>/codex/usage-N.json`](codex.md#usage-snapshots-on-disk), a separate, sanitized snapshot with its own `schemaVersion: 1`. It records last-good measurements and safe failure metadata without credentials. This does not change Codex command `--json` payloads or their in-memory cache behavior.

## Codex session fields

Codex list account rows carry an additive `session` object. This is preserved both in the bare `ccswap codex list --json` / `ccswap list --provider codex --json` payload and under `codex.accounts` in the merged list. `ccswap codex status --json` adds a `sessions` array of `{ "number": N, "session": ... }` for every saved slot, including slots without a session home. Existing active-account fields keep describing the default login.

```json
{"state": "live", "capturePending": true, "runCount": 1, "shared": false}
```

`state` is `absent`, `idle`, `live`, `uncertain`, `stale` or `conflict` (priority: conflict, uncertain, live, stale, idle, absent). `capturePending` is true for live/uncertain homes, unapplied committed intents or unadopted session credentials. `runCount` is a nonnegative count of alive supervisors, not a guessed writer-PID count; `shared` is true when an active run opted into sharing. Session fields contain no paths, token claims, fingerprints, environment or prompts. See [Codex session mode](codex.md#session-mode-codex-run) for launch and recovery behavior. `codex run` has no wrapper `--json`; Codex's own JSON arguments belong after `--`.
