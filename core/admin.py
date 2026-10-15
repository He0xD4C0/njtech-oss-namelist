from allauth.account.models import EmailAddress, EmailConfirmation
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group
from unfold.admin import ModelAdmin

from .models import (
    AuditEvent,
    PendingAvatarDeletion,
    PolicyConsent,
    PolicyDocument,
    RuntimeSetting,
    SiteState,
    SyncRun,
    User,
)


def navigation(request):
    """Use the registered model permissions for every business navigation link."""
    from django.urls import reverse

    groups = [{"title": "站点 / Site", "items": [
        {"title": "公开目录 / Directory", "icon": "home", "link": "/"},
        {"title": "个人中心 / Account", "icon": "person", "link": reverse("core:profile")},
    ]}]
    if request.user.is_superuser:
        groups[0]["items"].append({"title": "运行时配置 / Configuration", "icon": "settings", "link": reverse("core:configuration")})
    icons = {"core": "settings", "bulletins": "campaign", "repositories": "folder", "providers": "link", "directory": "leaderboard"}
    for app in admin.site.get_app_list(request):
        groups.append({"title": app["name"], "collapsible": True, "items": [
            {"title": model["name"], "icon": icons.get(app["app_label"], "table"), "link": model["admin_url"]}
            for model in app["models"] if model.get("admin_url") and model["object_name"] != "RuntimeSetting"
        ]})
    return groups


@admin.register(User)
class UserAdmin(BaseUserAdmin, ModelAdmin):
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("资料 / Profile", {"fields": ("email", "full_name", "public_email", "language", "quota")}),
        ("角色 / Role", {"fields": ("is_active", "is_staff", "is_superuser")}),
        ("时间 / Dates", {"fields": ("last_login", "date_joined")}),
    )
    readonly_fields = ("last_login", "date_joined")
    list_display = ("username", "email", "is_staff", "is_superuser", "quota")

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if obj.is_superuser:
            obj.is_staff = True
        if change and obj.pk == request.user.pk and (not obj.is_superuser or not obj.is_active):
            # Do not silently lock the active operator out of the only recovery role.
            from django.core.exceptions import ValidationError

            if not User.objects.filter(is_superuser=True, is_active=True).exclude(pk=obj.pk).exists():
                raise ValidationError("必须保留至少一位有效超管 / Keep at least one active superadmin")
        super().save_model(request, obj, form, change)


class PrivateReadOnlyAdmin(ModelAdmin):
    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RuntimeSetting)
class RuntimeSettingAdmin(PrivateReadOnlyAdmin):
    fields = ("key", "secret", "updated_at", "updated_by")
    list_display = ("key", "secret", "updated_at")
    readonly_fields = fields

    def changelist_view(self, request, extra_context=None):
        from django.shortcuts import redirect

        if not self.has_view_permission(request):
            from django.core.exceptions import PermissionDenied

            raise PermissionDenied
        return redirect("core:configuration")


for model in (PolicyDocument, PolicyConsent, AuditEvent, SyncRun, SiteState, PendingAvatarDeletion):
    admin.site.register(model, PrivateReadOnlyAdmin)

# Allauth's stock admins can expose unencrypted SocialApp/SocialToken rows, or allow
# bypassing independent mailbox verification. Their state is managed only by flows.
for model in (EmailAddress, EmailConfirmation, SocialAccount, SocialToken, SocialApp, Group):
    if admin.site.is_registered(model):
        admin.site.unregister(model)

# Keep the bundled sites/scheduler forms, actions and permissions, and supply
# Unfold's field rendering just as UserAdmin does above.
for model, model_admin in list(admin.site._registry.items()):
    if model._meta.app_label in {"sites", "django_celery_beat"}:
        admin.site.unregister(model)
        admin.site.register(model, type(f"Themed{type(model_admin).__name__}", (type(model_admin), ModelAdmin), {}))
