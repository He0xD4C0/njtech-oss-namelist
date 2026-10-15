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
