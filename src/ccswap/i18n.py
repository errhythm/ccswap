"""English-only stubs: the TUI still routes text through these hooks."""

import gettext


def mark(message: str) -> str:
    return message


def translator(language: str = "en") -> gettext.NullTranslations:
    return gettext.NullTranslations()


def localize_bindings(node, translate) -> None:
    """No-op: bindings are already English."""
