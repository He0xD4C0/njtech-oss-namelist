from django.apps import AppConfig


class GiteeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "providers.gitee"
    label = "gitee_oauth"

