"""Translation catalogs and per-app isolation; no real accounts or locale changes."""

import io
import json
from string import Formatter

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po

from claude_swap import i18n
from claude_swap.settings import load_ui_settings, set_setting


def fields(message):
    forms = (message,) if isinstance(message, str) else message
    return {
        (name, spec, conversion)
        for form in forms
        for _, name, spec, conversion in Formatter().parse(form)
        if name is not None
    }


def test_catalogs_are_complete_safe_and_compiled():
    with (i18n.LOCALE_DIR / "ccswap.pot").open(encoding="utf-8") as stream:
        template = read_po(stream)
    expected = {message.id for message in template if message.id}
    for language in i18n.LANGUAGES:
        if language == "en":
            continue
        root = i18n.LOCALE_DIR / language / "LC_MESSAGES"
        with (root / "ccswap.po").open(encoding="utf-8") as stream:
            catalog = read_po(stream, locale=language)
        assert {message.id for message in catalog if message.id} == expected
        for message in catalog:
            if not message.id:
                continue
            strings = (
                (message.string,) if isinstance(message.string, str) else message.string
            )
            assert strings and all(strings) and not message.fuzzy
            assert fields(message.id) == fields(message.string), message.id
            assert not list(message.check()), message.id
        compiled = io.BytesIO()
        write_mo(compiled, catalog)
        assert compiled.getvalue() == (root / "ccswap.mo").read_bytes()


def test_fallback_does_not_follow_environment_or_change_other_translators(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LANGUAGE", "el")
    german = i18n.translator("de")
    greek = i18n.translator("el")
    assert german.gettext("Settings…") == "Einstellungen…"
    assert greek.gettext("Settings…") == "Ρυθμίσεις…"
    assert i18n.translator("en").gettext("Settings…") == "Settings…"
    assert i18n.translator("unknown").gettext("Settings…") == "Settings…"
    assert german.gettext("Settings…") == "Einstellungen…"
    monkeypatch.setattr(i18n, "LOCALE_DIR", tmp_path)
    assert i18n.translator("de").gettext("Settings…") == "Settings…"
    root = tmp_path / "de/LC_MESSAGES"
    root.mkdir(parents=True)
    (root / "ccswap.mo").write_bytes(b"broken")
    assert i18n.translator("de").gettext("Settings…") == "Settings…"


def test_language_settings_default_validate_and_preserve_other_keys(tmp_path):
    assert load_ui_settings(tmp_path).language == "en"
    set_setting(tmp_path, "ui.mask", "on")
    set_setting(tmp_path, "ui.language", "el")
    assert load_ui_settings(tmp_path).language == "el"
    assert load_ui_settings(tmp_path).mask is True
    path = tmp_path / "settings.json"
    data = json.loads(path.read_text())
    data["ui"]["language"] = ["not", "a", "language"]
    path.write_text(json.dumps(data))
    assert load_ui_settings(tmp_path).language == "en"


def test_blank_binding_does_not_turn_into_gettext_catalog_metadata():
    from types import SimpleNamespace
    from textual.binding import Binding

    node = SimpleNamespace(
        _bindings=SimpleNamespace(
            key_to_bindings={"x": [Binding("x", "noop", show=False)]}
        ),
        refresh_bindings=lambda: None,
    )
    i18n.localize_bindings(node, i18n.translator("de").gettext)
    assert node._bindings.key_to_bindings["x"][0].description == ""


def test_binding_translation_is_idempotent_when_catalog_words_collide():
    from types import SimpleNamespace
    from textual.binding import Binding

    node = SimpleNamespace(
        _bindings=SimpleNamespace(
            key_to_bindings={"x": [Binding("x", "noop", "Back")]}
        ),
        refresh_bindings=lambda: None,
    )
    translate = lambda text: {
        "Back": "Zurück",
        "Zurück": "wrong second translation",
    }.get(text, text)
    i18n.localize_bindings(node, translate)
    i18n.localize_bindings(node, translate)
    assert node._bindings.key_to_bindings["x"][0].description == "Zurück"
