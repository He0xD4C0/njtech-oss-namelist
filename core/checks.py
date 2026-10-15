from django.conf import settings
from django.core.checks import Error, register


@register(deploy=True)
def encryption_key_check(app_configs, **kwargs):
    try:
        from .crypto import cipher

        cipher()
    except Exception:
        return [Error("APP_ENCRYPTION_KEY must be a valid server-provided Fernet key", id="core.E001")]
    return []


@register(deploy=True)
def source_availability_check(app_configs, **kwargs):
    if settings.DEBUG:
        return []
    try:
        from .git import validate_git_url

        validate_git_url(settings.SOURCE_REPOSITORY_URL, github_only=True)
    except (ValueError, AttributeError):
        return [
            Error(
                "Configure the public main GitHub SOURCE_REPOSITORY_URL before deploying this AGPL application",
                id="core.E002",
            )
        ]
    return []
