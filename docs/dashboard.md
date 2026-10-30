# Dashboard and menu bar

## Interactive dashboard (TUI)

Run `ccswap` on its own (or `ccswap tui`) for the full-screen dashboard: Claude Code and Codex appear together in labelled sections, with live usage, provider-correct switching, and auto-switching, all keyboard-driven. Arrow-key and Vim-style menu navigation wraps at both ends. The menu's **Settings…** screen cycles the theme, dashboard view, auto-switch threshold, and strategy, and toggles **Mask account info**. Masking hides email addresses and organization names on the dashboard, the `w` watch page, and the auto-switch view — including its candidate list and event log — while slot numbers and aliases stay visible so you can still switch. `m` toggles it from any screen, and the footer shows `Mask on` or `Mask off`. The same switch is `ccswap config set ui.mask on` (off by default). The dashboard view can show both providers or only Claude Code / Codex without stopping background updates for the hidden provider. `ccswap watch` opens straight into the live monitor using the selected dashboard view. Works on macOS, Linux, and Windows.

Hover over a menu option for a short explanation. With the keyboard, highlight
a row and press `?` to read the same help; Esc closes it without activating the option.

The dashboard also supports English, German and Greek. Choose **Settings → Language (restart)** or use `ccswap config set ui.language de` / `el`, then restart. See [translation contributions](translating.md) for additional languages.

## Menu bar (macOS)

Needs the `menubar` extra (macOS only):

```bash
uv tool install 'ccswap[menubar]'   # or: pipx install 'ccswap[menubar]'
ccswap menubar
```

Shows every account's 5h / 7d / spend usage and switches with a click (specific / rotate / best / next-available), plus the TUI's add / disable-enable / remove / refresh actions. Enable *Settings → Auto-switch accounts* to run the same engine as [`ccswap auto`](auto-switching.md) in the background; it shares the `autoswitch.*` settings, so the menu bar and CLI stay in sync. Off until you turn it on.

The icon is owned by a small wrapper bundle, `/Applications/ccswap Menubar.app`, that `ccswap menubar` builds (and refreshes after upgrades) on every start before hopping into it. That gives the process the bundle identifier `com.ccswap.menubar`, which is what menu bar managers (Dimble, Ice, Bartender, SaneBar) key on to list, hide and order an icon; a bare Python process has none and is invisible to them. It has to be `/Applications`: macOS resolves a status item's owner from the app's location, and for an app anywhere else the item's identity is nil, so the first time a manager hides anything the icon goes down with it. If `/Applications` is not writable the bundle lands in the data directory and you get a one-line warning. Set `CCSWAP_NO_BUNDLE=1` to run in the plain interpreter instead.

**Keep it running without a terminal.** `ccswap menubar` runs in the foreground, so the status item dies with the terminal that started it and does not come back after a reboot. `--install-service` hands it to launchd instead — starts at login, restarts on crash, no `.app` bundle:

```bash
ccswap menubar --install-service     # start now, and at every login
ccswap menubar --service-status      # installed? loaded? pid?
ccswap menubar --uninstall-service   # stop it and remove the plist
```

The agent lives at `~/Library/LaunchAgents/com.ccswap.menubar.plist` and logs to `~/Library/Logs/com.ccswap.menubar.{log,err}`. It pins the `ccswap` console script, whose path survives an upgrade — but the running process keeps the old build until it restarts, so after `ccswap upgrade` either re-run `--install-service` or `launchctl kickstart -k gui/$(id -u)/com.ccswap.menubar`.
