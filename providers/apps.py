from django.apps import AppConfig


class ProvidersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "providers"

    def ready(self):
        from . import signals  # noqa: F401

