from django.apps import AppConfig


class BulletinsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "bulletins"

    def ready(self):
        from django.contrib import admin

        # Unfold's dashboard bypasses admin/base_site.html. Keep its original layout
        # and add the same announcement footer on both dashboard entrypoints.
        if admin.site.index_template is None:
            admin.site.index_template = "bulletins/admin_index.html"
        if admin.site.app_index_template is None:
            admin.site.app_index_template = "bulletins/admin_app_index.html"
