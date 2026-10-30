# Automatic switching before you hit a rate limit

Let ccswap watch your usage and switch for you. When the active account's 5-hour or 7-day window reaches the threshold (default 90%), it switches to the account with the most quota left — before you hit the limit, and safe to run while Claude Code is working:

```bash
ccswap auto                     # foreground loop, polls every 60s
ccswap auto --threshold 80      # switch earlier
ccswap auto --model Fable       # also switch when the Fable weekly limit is hit
ccswap auto --once              # single check-and-switch, for cron/scripts
ccswap auto --dry-run           # log what it would do, never switch
ccswap auto --strategy consume-first   # burn the soonest-resetting account first
```

## Optional five-hour window warming

A fresh five-hour window starts with a real request. To start idle Claude
accounts' windows before you need them, explicitly enable warming:

```bash
ccswap config set autoswitch.warmupFiveHour true
ccswap auto --once --dry-run     # preview eligible accounts; no warmup requests
ccswap auto                     # keep running for automatic warming
```

Disable it with `ccswap config set autoswitch.warmupFiveHour false`. Restart
an already running auto process after changing this setting. It is **off by
default**, applies only to Claude, and sends real subscription requests.
The fixed prompt is `Reply OK.`, using the `haiku` model alias and one turn;
Claude Code's normal request overhead still applies. It does not create
additional quota or promise that every model-specific window will start.

While enabled, the auto process wakes at least once a minute, even with a
longer configured interval. It checks cached readings at most once a minute
and sends at most one warmup request per check. Normal usage polling and its
backoff remain in charge: warming never forces a usage refresh. Disabled,
API-key, incomplete, or unidentifiable accounts are skipped. A running window
is skipped even at 0%. Usage must be no more than two minutes old, read after
the previous reset, and show weekly headroom; exhausted Haiku limits also
prevent warming. Unknown or expired access tokens are skipped rather than
refreshed by warming. This conservative policy can delay warming when usage
is unavailable or polling is slow.

The request runs in a disposable profile and working directory, with no shared
user/project settings, tools, skills, MCP servers or user hooks. The child
receives only the verified access token; no refresh token is copied or spent,
and the global login is not switched. Administrator-managed Claude policies
still apply, including managed hooks and routing: enable warming only where
those policies permit unattended requests. The CLI must support the flags
listed in [its reference](https://code.claude.com/docs/en/cli-reference).

`warmup` events appear in the auto log and JSONL output. A completed CLI request
is not independent verification of the server's new reset time; subsequent
normal quota polling supplies that reading. Before launching, ccswap records
an account-and-organization guard in `warmup.json` in its data directory.
Concurrent auto processes share it, and slot renumbering does not bypass it.
Timeouts, crashes and unsuccessful responses conservatively retain the guard
for five hours plus the one-minute request timeout. Do not delete this file
to retry an uncertain request. Guards are local to this machine.
Warming is synchronous: the identity probe has a five-second timeout and the
request a one-minute timeout, delaying the next auto-switch check. CLI wrappers
may have child processes that outlive that timeout and delay cleanup further;
the persistent guard still prevents retries.

## How it behaves & advanced usage

- Runs safely alongside Claude Code: switches take the same credential locks Claude Code uses, so a swap never collides with a token refresh.
- A cooldown (default 5 min) and a hysteresis margin stop it flip-flopping near the threshold: a proactive switch only lands on an account that's below the threshold *and* better than the current one by the margin — a candidate that clears the margin is always taken, but two accounts hovering at the line never ping-pong. When every account is exhausted it keeps checking on a bounded slow cadence, waking sooner for an imminent reset.
- **Strategies** (`--strategy`, or `ccswap config set autoswitch.strategy`): `best` (default) stays put until the active account nears its limit, then moves to the account with the most quota left. `consume-first` proactively keeps you on the account whose **weekly window resets soonest** — use-it-or-lose-it — switching to a sooner-resetting account (with room to spare) even below the threshold, so perishable weekly quota isn't wasted.
- Usage polling is adaptive — a couple of accounts per check, busy alternates watched more closely, and exhausted ones checked about every ten minutes (or slower after 429s) — so API traffic stays flat no matter how many accounts you manage.
- It fails safe: if a usage check errors it keeps trusting the last-known numbers while retries back off, and an expired token on an idle machine makes it hold rather than fail over (Claude Code refreshes the token on your next message).
- An account whose refresh token has died is quarantined and reported until you either log in with it and re-run `ccswap add --slot N`, or replace its stored credentials from a known-good export — a plain `ccswap import backup.cswap` replaces dead-token slots on its own (`--force` is still required to replace other existing accounts; note a stale export can carry an already-superseded token). API-key accounts are never rotated onto unless you pass `--include-api-key-accounts`.
- To hold an account out of rotation yourself — a work account you don't want touched, one you're resting — run `ccswap disable <num|email>`; `ccswap enable <num|email>` puts it back. Disabled accounts are skipped by auto-switch, bare `ccswap switch`, and the `best` / `next-available` strategies, but stay fully managed and remain a valid explicit `ccswap switch <num|email>` target. They show a `(disabled)` marker in `ccswap list`, in the [TUI](dashboard.md#interactive-dashboard-tui), and in the [menu bar](dashboard.md#menu-bar-macos) — both of which also let you toggle the state in place (TUI: menu → *Disable / enable account…*; menu bar: *Disable / enable account*).
- By default only the account-wide 5h/7d windows drive switching, and they share one threshold. If you work on one model and hit its **weekly per-model limit** first (e.g. Fable), add `--model Fable` (or `ccswap config set autoswitch.model Fable`) to fold that model's window into the decision, so it switches off an account whose model quota is spent even while its 5h/7d windows still have room.
- **Separate gates** (`5h`, weekly `7d`, and any named model such as Fable): any one of them reaching its own threshold switches, even if the others are fine. Each window takes the first key that is set: 5h `threshold5h`; 7d `threshold7d`, then `weeklyThreshold`; a model window `modelThresholds`, then `weeklyThreshold`; then `autoswitch.threshold`. `weeklyThreshold` (`--weekly-threshold`) is upstream claude-swap's weekly line, kept as an alias so their configs work here. Codex auto-switching also honors `autoswitch.strategy`, `autoswitch.threshold5h`, and `autoswitch.threshold7d` (its Weekly window); model gates do not apply to Codex.

```bash
ccswap config set autoswitch.threshold5h 85    # session window
ccswap config set autoswitch.threshold7d 70    # weekly window
ccswap config set autoswitch.modelThresholds Fable=40   # Fable=40,Opus=60 for several
ccswap config set autoswitch.weeklyThreshold 97 # 7d + model windows without their own key
ccswap config unset autoswitch.threshold5h     # back to inheriting autoswitch.threshold
```

The TUI settings menu cycles the same three gates (inherit, then 40–95%). On the auto-switch view, `t` opens a popup for the same numbers: ↑↓ moves between the shared threshold (this session only) and the 5h, weekly, and Fable gates (saved immediately); ←→ changes the selected value by 1%, and ⌫ returns a gate to inherit. `ccswap auto --threshold-5h 85 --threshold-7d 70 --model-thresholds Fable=40` overrides them for one run. Naming a model in `modelThresholds` makes it a gate even without `autoswitch.model`. `autoswitch.windows` still chooses whether 5h, 7d, or both are gates at all. Model names are Anthropic's own per-model `display_name`s, matched case-insensitively; the exact strings are the per-model rows in `ccswap list` (e.g. `Fable: 100%`).
- **Which window binds** (`ccswap config set autoswitch.windows <both|5h|7d>`, default `both`): narrows the decision to just one of the two account-wide windows. Set it to `5h` to switch only on the rolling session window and never on the weekly one — for someone happy to run their weekly quota all the way down. Set it to `7d` for the opposite: ignore the session window, switch only when the weekly one is the problem. `ccswap list` and `cswap watch` keep showing both numbers regardless — this only changes what `auto` decides on.

For cron/systemd timers, `--once` reports the outcome in its exit code (`0` switched, `1` error, `2` nothing to do, `3` blocked — no viable target), and `--json` emits one JSON event per line:

```bash
*/5 * * * * ccswap auto --once --json >> ~/.ccswap-auto.log 2>&1
```

Defaults like the threshold and cooldown are configurable with `ccswap config set autoswitch.threshold 80` — flags override them (see [Configuration](configuration.md#configuration)).
