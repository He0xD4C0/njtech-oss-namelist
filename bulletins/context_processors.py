from core.markdown import render_markdown

from .services import pending_announcements


def announcements(request):
    return {
        "pending_announcements": [
            {"announcement": item, "rendered_body": render_markdown(item.body)}
            for item in pending_announcements(request.user)
        ]
    }
