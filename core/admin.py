import json

from allauth.account.models import EmailAddress, EmailConfirmation
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils.html import format_html
from unfold.admin import ModelAdmin

from .i18n import management_message
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
    groups = [{"title": "常用入口", "items": [
        {"title": "管理首页", "icon": "dashboard", "link": reverse("admin:index"), "active": request.path_info == reverse("admin:index")},
        {"title": "公开目录", "icon": "home", "link": "/", "active": request.path_info == "/"},
        {"title": "个人中心", "icon": "person", "link": reverse("core:profile")},
    ]}]
    if request.user.is_superuser:
        groups[0]["items"].append({"title": "站点配置", "icon": "settings", "link": reverse("core:configuration")})
    icons = {"core": "settings", "bulletins": "campaign", "repositories": "folder", "providers": "link", "directory": "leaderboard"}
    for app in admin.site.get_app_list(request):
        groups.append({"title": app["name"], "collapsible": True, "items": [
            {"title": model["name"], "icon": icons.get(app["app_label"], "table"), "link": model["admin_url"]}
            for model in app["models"] if model.get("admin_url") and model["object_name"] != "RuntimeSetting"
        ]})
    return groups


def dashboard(request, context):
    from repositories.models import Submission

    context["title"] = "管理首页"
    cards = []
    if request.user.is_staff:
        for status, title in (("needs_review", "待处理申请"), ("queued", "待机审申请"), ("published", "公开仓库")):
            cards.append({"title": title, "count": Submission.objects.filter(status=status).count(),
                          "url": reverse("admin:repositories_submission_changelist") + f"?status__exact={status}"})
    if request.user.is_superuser:
        cards.append({"title": "失败任务", "count": SyncRun.objects.filter(status__in=("failed", "queue-failed")).count(),
                      "url": reverse("admin:core_syncrun_changelist") + "?status=failed"})
    context["overview_cards"] = cards
    return context


def formatted_json(value):
    return format_html('<pre class="admin-evidence">{}</pre>', json.dumps(value, ensure_ascii=False, indent=2))


@admin.register(User)
class UserAdmin(BaseUserAdmin, ModelAdmin):
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("个人资料", {"fields": ("email", "full_name", "public_email", "language", "quota")}),
        ("账号权限", {"fields": ("is_active", "is_staff"), "description": "管理员可管理公告与查看机审证据；站点配置仅限超级管理员。"}),
        ("账号时间", {"fields": ("last_login", "date_joined")}),
    )
    readonly_fields = ("username", "email", "last_login", "date_joined")
    list_display = ("username", "full_name", "email", "is_active", "is_staff", "is_superuser", "quota")
    search_fields = ("username", "full_name", "email")
    list_filter = ("is_active", "is_staff", "is_superuser")
    list_per_page = 50
    warn_unsaved_form = True

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if "language" in form.base_fields:
            form.base_fields["language"].choices = [("", "跟随公开站点"), ("zh-hans", "简体中文"), ("en", "英语")]
            form.base_fields["language"].help_text = "仅影响该用户的公开站点界面；管理平台固定使用中文。"
        if "quota" in form.base_fields:
            form.base_fields["quota"].help_text = "该用户可保留的仓库申请数量上限。"
        return form

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

    def get_readonly_fields(self, request, obj=None):
        return self.readonly_fields + (("is_active", "is_staff") if obj and obj.is_superuser else ())

    def save_model(self, request, obj, form, change):
        # Read-only identity fields must not be written back from a stale form.
        obj.save(update_fields=["full_name", "public_email", "language", "quota", "is_active", "is_staff"])


class PrivateReadOnlyAdmin(ModelAdmin):
    list_per_page = 50
    ordering = ("-pk",)

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


@admin.register(PolicyDocument)
class PolicyDocumentAdmin(PrivateReadOnlyAdmin):
    list_display = ("document_kind", "document_language", "source_sha", "updated_at")
    list_filter = ("language",)
    search_fields = ("source_sha", "body")
    fields = ("document_kind", "document_language", "source_sha", "body", "updated_at")
    readonly_fields = ("document_kind", "document_language")

    @admin.display(description="政策类型", ordering="kind")
    def document_kind(self, obj):
        return {"privacy": "隐私政策", "terms": "使用条款", "inclusion": "收录规则"}.get(obj.kind, obj.kind)

    @admin.display(description="正文语言", ordering="language")
    def document_language(self, obj):
        return {"zh-CN": "简体中文", "en-US": "英语"}.get(obj.language, obj.language)


@admin.register(PolicyConsent)
class PolicyConsentAdmin(PrivateReadOnlyAdmin):
    list_display = ("user", "language", "created_at")
    list_select_related = ("user",)
    search_fields = ("user__username", "user__email")
    list_filter = ("language",)


@admin.register(AuditEvent)
class AuditEventAdmin(PrivateReadOnlyAdmin):
    list_display = ("event_action", "subject", "actor", "created_at")
    list_select_related = ("actor",)
    search_fields = ("subject", "action", "actor__username")
    date_hierarchy = "created_at"
    fields = ("actor", "event_action", "subject", "event_detail", "created_at")
    readonly_fields = ("event_action", "event_detail")

    @admin.display(description="操作", ordering="action")
    def event_action(self, obj):
        return {"setting.updated": "更新配置", "site.activated": "激活站点", "platform.disconnected": "解绑平台",
                "account.deleted": "注销账号", "backup.settings.saved": "保存备份配置",
                "backup.test.requested": "测试备份连接", "backup.apply.requested": "启用备份",
                "backup.backup.requested": "执行全量备份"}.get(obj.action, obj.action)

    @admin.display(description="详情")
    def event_detail(self, obj):
        return formatted_json(obj.detail)


class TaskStatusFilter(admin.SimpleListFilter):
    title = "任务状态"
    parameter_name = "status"

    def lookups(self, request, model_admin):
        return (("failed", "执行或提交失败"), ("running", "等待或执行中"), ("success", "已完成"))

    def queryset(self, request, queryset):
        statuses = {"failed": ("failed", "queue-failed"), "running": ("pending", "queued", "running", "retrying"),
                    "success": ("success", "unchanged")}
        return queryset.filter(status__in=statuses[self.value()]) if self.value() in statuses else queryset


@admin.register(SyncRun)
class SyncRunAdmin(PrivateReadOnlyAdmin):
    list_display = ("task_kind", "task_status", "task_message", "created_at")
    list_filter = (TaskStatusFilter,)
    search_fields = ("message", "source_sha")
    fields = ("task_kind", "task_status", "task_message", "source_sha", "created_at", "lease_expires_at")
    readonly_fields = ("task_kind", "task_status", "task_message")

    @admin.display(description="结果说明")
    def task_message(self, obj):
        return management_message(obj.message) or "—"

    @admin.display(description="任务类型", ordering="kind")
    def task_kind(self, obj):
        from .initialization import TASK_KIND_LABELS

        return TASK_KIND_LABELS.get(obj.kind, obj.kind)

    @admin.display(description="任务状态", ordering="status")
    def task_status(self, obj):
        from .initialization import SYNC_LABELS

        return SYNC_LABELS.get(obj.status, (obj.status,))[0]


@admin.register(SiteState)
class SiteStateAdmin(PrivateReadOnlyAdmin):
    list_display = ("id", "activated_at", "bootstrap_user")


@admin.register(PendingAvatarDeletion)
class PendingAvatarDeletionAdmin(PrivateReadOnlyAdmin):
    list_display = ("key", "created_at")
    search_fields = ("key",)

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
