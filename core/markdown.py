import re

import nh3
from django.utils.safestring import mark_safe
from markdown_it import MarkdownIt

parser = MarkdownIt("commonmark", {"html": False, "linkify": False})


def render_markdown(body, *, text_replacements=None):
    environment = {}
    tokens = parser.parse(body, environment)
    if text_replacements:
        pattern = re.compile("|".join(re.escape(key) for key in text_replacements))
        for token in tokens:
            for child in token.children or []:
                if child.type == "text":
                    # Substitute plain text tokens only; values cannot become Markdown,
                    # HTML, link targets or executable template expressions.
                    child.content = pattern.sub(lambda match: text_replacements[match[0]], child.content)
    return mark_safe(
        nh3.clean(
            parser.renderer.render(tokens, parser.options, environment),
            tags={
                "p",
                "br",
                "hr",
                "em",
                "strong",
                "s",
                "a",
                "blockquote",
                "code",
                "pre",
                "ul",
                "ol",
                "li",
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
                "table",
                "thead",
                "tbody",
                "tr",
                "td",
                "th",
            },
            attributes={"a": {"href", "title"}, "ol": {"start"}},
            url_schemes={"https", "http", "mailto"},
            link_rel="nofollow noopener noreferrer",
        )
    )
