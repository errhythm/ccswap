# Privacy and usage stats

ccswap sends nothing about you or your accounts anywhere. The only network calls it makes on its own behalf are the provider APIs it reads usage from and, once a day, PyPI's version check (`updates.check`).

## Anonymous usage stats (opt-in)

Starting with 0.54.0b2, the dashboard asks once whether you want to share anonymous usage stats. They are **off until you say yes**, and the answer is saved either way. Change it any time:

```bash
ccswap config set telemetry.enabled on    # or off
```

These always turn it off, whatever the setting says: `DO_NOT_TRACK=1`, `CCSWAP_TELEMETRY=0`, or a `CI` environment variable.

### What one event contains

| Field | Example | Notes |
|---|---|---|
| `distinct_id` | `3f9c…` | A random ID made on this machine and stored in `cache/telemetry.json`. It isn't derived from anything about you. |
| `command` | `switch` | A name from a fixed list in [`telemetry.py`](../src/ccswap/telemetry.py). Anything else is sent as `other`. Arguments are never sent. |
| `provider` | `codex` | Only when the command starts with `claude`, `codex` or `agy`. |
| `version` | `0.54.0b2` | The ccswap version. |
| `os` | `Darwin` | `platform.system()`. |
| `python` | `3.13.1` | The Python version. |

**Never sent:** emails, account names, aliases, organization names, tokens, file paths, directory mappings, usage numbers, command arguments, or anything typed into Claude Code, Codex or Antigravity.

### When events are sent

- At most one event per command per hour. Cron jobs and the Raycast extension call ccswap often.
- Never for `--json` output, `ccswap run`, `ccswap statusline` or `ccswap hub`.
- The send runs in the background with a 2-second timeout. A failed send is dropped, never retried, and never affects the command.

### Where they go

Events go to a PostHog project in the EU region. It is set to discard IP addresses and skip GeoIP lookup, and every event also asks PostHog to skip GeoIP and not build a person profile.
