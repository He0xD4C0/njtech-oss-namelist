from django.utils.translation import get_language


def text(zh, en):
    return en if (get_language() or "").startswith("en") else zh


def management_message(value):
    """Show the Chinese part of persisted bilingual system messages in management."""
    return value.partition(" / ")[0]
