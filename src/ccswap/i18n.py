"""Per-app gettext catalogs; no global locale or builtins changes."""

import gettext
import struct
from dataclasses import replace
from pathlib import Path

LANGUAGES = {"en": "English", "de": "Deutsch", "el": "Ελληνικά"}
LOCALE_DIR = Path(__file__).with_name("locale")


def mark(message: str) -> str:
    """Mark text that is translated later, such as binding descriptions."""
    return message


def translator(language: str) -> gettext.NullTranslations:
    if not isinstance(language, str) or language not in LANGUAGES or language == "en":
        return gettext.NullTranslations()
    try:
        return gettext.translation(
            "ccswap", localedir=LOCALE_DIR, languages=[language], fallback=True
        )
    except (OSError, EOFError, ValueError, LookupError, struct.error):
        return gettext.NullTranslations()


def localize_bindings(node, translate) -> None:
    if getattr(node, "_ccswap_bindings_localized", False):
        return
    # Same Textual binding map already used by the app's live mask label.
    for key, bindings in node._bindings.key_to_bindings.items():
        node._bindings.key_to_bindings[key] = [
            replace(binding, description=translate(binding.description))
            if binding.description
            else binding
            for binding in bindings
        ]
    node._ccswap_bindings_localized = True
    node.refresh_bindings()
