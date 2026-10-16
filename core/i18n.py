from django.utils.translation import get_language


def text(zh, en):
    return en if (get_language() or "").startswith("en") else zh
