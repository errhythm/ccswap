# T3 Code hub

`ccswap hub` serves your Claude Code and Codex quota over the same management API that a [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) hub uses. Any app that can read a CLIProxyAPI hub, such as T3 Code, can then show the usage of every account ccswap manages.

The hub is read-only. It reports usage and never switches accounts, sends prompts, or hands out tokens.

## Set it up

On macOS, install the hub as a launchd service so it starts at login and restarts if it crashes. You don't need a terminal open:

```bash
ccswap hub install     # start now, and at every login
ccswap hub key         # print the management key
```

Then in T3 Code, open Settings, choose **Add a CLIProxyAPI hub**, and enter:

| Field | Value |
| --- | --- |
| Hub URL | `http://127.0.0.1:8317` |
| Management key | the output of `ccswap hub key` |

Each ccswap account then appears in T3 Code with its 5-hour, weekly and per-model limits.

If a real CLIProxyAPI already uses port 8317, pick another one and use it in the URL:

```bash
ccswap hub install --port 9000
```

## Commands

```bash
ccswap hub install [--host H] [--port P]   # run the hub as a launchd service (macOS)
ccswap hub status                          # installed? loaded? pid?
ccswap hub uninstall                       # stop the service and remove its plist
ccswap hub serve [--host H] [--port P]     # run it in this terminal instead (any OS)
ccswap hub key                             # print the management key, creating it on first use
```

The service is `com.ccswap.hub`. Its plist is `~/Library/LaunchAgents/com.ccswap.hub.plist` and it logs to `~/Library/Logs/com.ccswap.hub.{log,err}`. Like the [menu bar service](dashboard.md#menu-bar-macos), it pins the `ccswap` console script. After `ccswap upgrade`, re-run `ccswap hub install` or `launchctl kickstart -k gui/$(id -u)/com.ccswap.hub`.

On Linux and Windows, run `ccswap hub serve` under your own service manager (systemd, a scheduled task, tmux).

## How it works

The hub answers the two routes T3 Code calls under `/v0/management/`:

- `GET auth-files` lists your Claude and Codex accounts. Each call runs one normal ccswap usage pass, the same one `ccswap list` runs, so the usage store still decides when Anthropic or OpenAI gets polled. API-key slots are left out because they have no subscription quota. Accounts you disabled with `ccswap disable` are still listed, because disabling only keeps an account out of auto-rotation.
- `POST api-call` is where a real CLIProxyAPI forwards a request upstream using the account's token. ccswap doesn't forward anything. It answers the Claude usage URL (`api.anthropic.com/api/oauth/usage`) and the Codex usage URL (`chatgpt.com/backend-api/wham/usage`) from that same pass, rebuilt in the provider's own response format. Every other URL or method gets a 404.

Because of this, the management key can read usage percentages and reset times and nothing else. No OAuth token leaves ccswap.

T3 Code's reset-credit button for Codex isn't supported. The hub reports no reset credits, and you can still redeem one with `ccswap codex redeem-reset`. Antigravity accounts aren't served yet.

## Security

- The key is a random 43-character token in `hub-key` inside the ccswap data directory (`~/.claude-swap-backup` on macOS), with mode 0600. Every request needs it as `Authorization: Bearer <key>` or `X-Management-Key: <key>`, otherwise the hub returns 401. To rotate the key, delete the file, run `ccswap hub key`, restart the hub, and paste the new key into T3 Code.
- The hub listens on `127.0.0.1` by default, so only this machine can reach it. `--host 0.0.0.0` exposes it to your network over plain HTTP, and the key travels in clear text, so ccswap prints a warning. To reach the hub from another machine, prefer an SSH tunnel (`ssh -L 8317:127.0.0.1:8317 your-mac`) or a TLS reverse proxy.
- The hub handles one request at a time with a 10-second timeout, so a stalled client can't block it.

## Switching accounts

T3 Code's hub integration only reads quota, so the hub can't switch accounts. Use `ccswap switch`, `ccswap auto` or the menu bar as usual. T3 Code's Claude sessions use the same login ccswap swaps, so they pick up the new account, and the hub reports its usage.
