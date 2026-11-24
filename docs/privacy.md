# Privacy and usage stats

ccswap sends nothing about you or your accounts anywhere. The only network calls it makes on its own behalf are the provider APIs it reads usage from and, once a day, PyPI's version check (`updates.check`).

## Anonymous usage stats (opt-in)

Starting with 1.0.0, ccswap asks once whether you want to share anonymous usage stats: in the dashboard, or on the first interactive command in a terminal (never under `--json`, in scripts or in CI). They are **off until you say yes**, and the answer is saved either way. Change it any time:

```bash
ccswap config set telemetry.enabled on    # or off
```

These always turn it off, whatever the setting says: `DO_NOT_TRACK=1`, `CCSWAP_TELEMETRY=0`, or a `CI` environment variable.

### What the events contain

Every event carries `distinct_id`, `version`, `os` and `python`. A `command` event adds `command` and `provider`. A `daily` event adds the switch and account counts.

| Field | Example | Notes |
|---|---|---|
| `distinct_id` | `3f9c…` | A random ID made on this machine and stored in `cache/telemetry.json`. It isn't derived from anything about you. |
| `command` | `switch` | A name from a fixed list in [`telemetry.py`](../src/ccswap/telemetry.py). Anything else is sent as `other`. Arguments are never sent. |
| `provider` | `codex` | Only when the command starts with `claude`, `codex` or `agy`. |
| `version` | `1.0.0` | The ccswap version. |
| `os` | `Darwin` | `platform.system()`. |
| `python` | `3.13.1` | The Python version. |
| `switches_claude`, `switches_codex`, `switches_agy` | `4` | `daily` only. Switches in the switch log since the previous `daily` event, manual and automatic. |
| `accounts_claude`, `accounts_codex`, `accounts_agy` | `3-5` | `daily` only. Saved accounts per provider, grouped as `0`, `1`, `2`, `3-5` or `6+`. |

**Never sent:** emails, account names, aliases, organization names, tokens, file paths, directory mappings, usage numbers, command arguments, or anything typed into Claude Code, Codex or Antigravity.

### When events are sent

- At most one `command` event per command per hour. Cron jobs and the Raycast extension call ccswap often.
- At most one `daily` event per day, sent on the first command after a day has passed.
- Never for `--json` output, `ccswap run`, `ccswap statusline` or `ccswap hub`.
- The send runs in the background with a 2-second timeout. A failed send is dropped, never retried, and never affects the command.

### Where they go

Events go to a PostHog project in the EU region. It is set to discard IP addresses and skip GeoIP lookup, and every event also asks PostHog to skip GeoIP and not build a person profile.
