from django import template
from django.utils.translation import get_language

register = template.Library()


@register.simple_tag
def ui(zh, en):
    return en if (get_language() or "").startswith("en") else zh
