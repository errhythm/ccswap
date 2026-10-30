"""Explanations shared by menu tooltips and keyboard help."""

OPTION_HELP = {
    "switch": "Choose a saved account to use. Codex must be restarted after switching to load the selected login.",
    "watch": "Monitor account quotas without switching. Press s in Watch to enable account selection.",
    "auto": "Open the auto-switch view for a provider. It starts as a dry-run preview; press l and confirm to switch live. Closing the view stops its engine.",
    "add-menu": "Save your current login, or add a Claude setup token or API key, as a managed account.",
    "disable-menu": "Exclude an account from automatic selection without deleting its saved login. You can enable it again here.",
    "remove-menu": "Choose an account whose saved credentials and configuration backup will be deleted. You will be asked to confirm.",
    "settings-menu": "Change appearance, account masking, switching policy and Claude Code integrations. Settings are saved for later runs.",
    "quit": "Close this app. Any auto-switch engine started by this app stops; separately running auto processes are not stopped.",
    "back": "Return to the previous menu without changing a setting.",
    "setting:theme": "Cycle dark, light and auto. Auto follows the terminal background.",
    "setting:view": "Cycle both providers, Claude only and Codex only. This filters the dashboard, Switch and Watch lists; administrative menus still show all accounts.",
    "setting:mask": "Hide account emails and organization names in supported displays. This does not remove credentials or change the active login.",
    "setting:threshold": "Cycle the default utilization threshold for automatic switching. More specific gates override it; weekly gates may inherit weeklyThreshold first. Disabled windows do not trigger switching.",
    "setting:threshold5h": "Cycle the 5-hour gate, then inherit. Automatic switching can trigger when any configured gate reaches its threshold.",
    "setting:threshold7d": "Cycle the weekly gate, then inherit. Applies to Claude's 7-day window and Codex's weekly window.",
    "setting:fable": "Cycle the Claude-only Fable weekly-model gate, then inherit. Other model thresholds are preserved. Exact values and additional models are available through ccswap config set.",
    "setting:strategy": "Cycle best and consume-first. Best favors quota headroom; consume-first proactively uses available quota whose weekly window resets sooner, even below the threshold. This does not redeem banked resets.",
    "claude:claude.statusline": "Install or remove ccswap's Claude Code statusline. Turning it off restores the statusline configuration backed up during installation.",
    "claude:claude.mod": "Enable or disable the ccswap Claude Code plugin. This may invoke Claude's plugin manager; it does not change your account.",
    "add-token": "Add a Claude setup token or API key. API-key requests are billed by usage; a setup token uses its account subscription.",
    "add-login": "Save the provider's current login as a managed account. An already managed login is refreshed in place.",
    "remove": "Remove this account's saved credentials and configuration backup after confirmation. Removal cannot be undone from this menu.",
    "disable": "Toggle whether this account may be selected automatically. Its saved login remains stored.",
}


def option_help(action_id: str) -> str | None:
    return OPTION_HELP.get(action_id) or OPTION_HELP.get(action_id.split(":", 1)[0])
