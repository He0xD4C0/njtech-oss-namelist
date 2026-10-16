from .avatar import avatar_available, avatar_url
from .branding import logo_url
from .runtime import get_setting, provider_enabled, registration_issues


def site_context(request):
    from django.conf import settings

    return {
        "site_initializing": getattr(request, "site_initializing", False),
        "brand_logo_url": logo_url(),
        "site_title": "南京工业大学开源软件仓库名录",
        "turnstile_site_key": get_setting("turnstile_site_key"),
        "source_url": settings.SOURCE_REPOSITORY_URL or "/source/",
        "enabled_providers": [p for p in ("github", "gitee", "gitlab") if provider_enabled(p)],
        "registration_issues": registration_issues(),
        "avatar_enabled": avatar_available(),
        "current_avatar_url": avatar_url(request.user) if request.user.is_authenticated else "",
    }
