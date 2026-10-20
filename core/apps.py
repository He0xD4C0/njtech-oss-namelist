from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"
    verbose_name = "站点管理"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks, signals  # noqa: F401
