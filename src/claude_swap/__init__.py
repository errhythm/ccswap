"""Old import name, kept so launchd services and app bundles made by ccswap < 0.54 still start.

They run ``python -m claude_swap``. Re-running ``ccswap <service> --install-service``
writes the new form; this shim can go once those installs have had time to upgrade.
"""
