# TUI translations

English is the default. Choose a language in **Settings → Language (restart)**,
or use `ccswap config set ui.language de` (German) / `el` (Greek), then restart
the dashboard. `en` restores English. The choice does not change your operating
system locale, command names, account identities or stored policy values.

Navigation, settings, confirmation dialogs, footer descriptions and the TUI's
own display messages are translated. Raw action output and backend diagnostics
remain in their original language. Keyboard keys stay the same. Missing messages
or unavailable catalogs fall back to English.

## Contribute a language

The standard gettext source files live in `src/claude_swap/locale`. Babel is a
**development-only** dependency; installed ccswap uses Python's standard library.

1. Add the language code and its native name to `LANGUAGES` in
   `src/claude_swap/i18n.py`. Config validation and the Settings cycle use this
   same register.
2. Create a catalog (replace `fr` with your language):

   ```bash
   uv run pybabel init -D ccswap -i src/claude_swap/locale/ccswap.pot -d src/claude_swap/locale -l fr
   ```

3. Translate the `.po` file. Keep named placeholders, format specifications,
   command examples and actual keyboard keys intact. Review fuzzy entries before
   removing their fuzzy flag. Use gettext plural messages when adding counters;
   do not build sentences from translated fragments.
4. Compile and test, then commit both `.po` and `.mo` files:

   ```bash
   uv run pybabel compile -D ccswap -d src/claude_swap/locale
   uv run pytest tests/test_i18n.py
   ```

For changed TUI text, mark a complete template with `self.app.gettext(...)`
(`self.gettext(...)` inside the app). Format named values **after** translation.
Pure display helpers receive a translator explicitly; `mark(...)` marks labels
such as class-level key bindings for translation at mount time.

```bash
uv run pybabel extract -k tr -k mark --project ccswap -o src/claude_swap/locale/ccswap.pot src/claude_swap/tui
uv run pybabel update -D ccswap -i src/claude_swap/locale/ccswap.pot -d src/claude_swap/locale
```

The catalog tests reject missing/empty messages, changed placeholders, fuzzy
translations and stale compiled files. Check long translations in a narrow
terminal as well as the normal dashboard.
