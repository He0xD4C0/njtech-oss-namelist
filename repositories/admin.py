from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from unfold.admin import ModelAdmin

from core.admin import formatted_json

from .models import AuditDispatch, AuditResult, LicensePolicy, Repository, StarSnapshot, Submission


class EvidenceAdmin(ModelAdmin):
    list_per_page = 50
    ordering = ("-pk",)

    def has_module_permission(self, request):
        return request.user.is_staff

    def has_view_permission(self, request, obj=None):
        return request.user.is_staff

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Submission)
class SubmissionAdmin(EvidenceAdmin):
    list_display = ("repository", "user", "status", "reason_summary", "changed_at")
    list_select_related = ("repository", "user")
    list_filter = ("status", "repository__platform", "repository__archived")
    search_fields = ("repository__full_name", "user__username")
    actions = ["recheck"]
    search_help_text = "按仓库完整名称或申请人的用户名搜索。"
    fieldsets = (
        ("收录状态", {"fields": ("repository", "user", "status", "reason", "license_labels", "changed_at")}),
        ("机审证据", {"fields": ("audited_head", "audited_policy", "audited_detector", "generation", "audit_history"),
                      "description": "请根据机审证据排查原因。许可证规则由独立策略仓库维护，同步后可重新运行机审。"}),
        ("收录与统计", {"fields": ("created_at", "published_at", "baseline_at", "baseline_stars"), "classes": ("collapse",)}),
    )
    readonly_fields = ("audit_history",)

    @admin.display(description="处理原因")
    def reason_summary(self, obj):
        return obj.reason[:100] or "—"

    @admin.display(description="最近五次机审")
    def audit_history(self, obj):
        return format_html_join("", '<p><a href="{}">第 {} 批 · {} · {}</a></p>', (
            (reverse("admin:repositories_auditresult_change", args=[result.pk]), result.generation,
             dict(Submission.Status.choices).get(result.decision, result.decision), result.created_at)
            for result in obj.audits.order_by("-created_at", "-pk")[:5]
        )) or "暂无机审结果，提交任务后请等待任务服务执行。"

    @admin.action(description="重新运行机审", permissions=["view"])
    def recheck(self, request, queryset):
        from .tasks import schedule_audit

        count = sum(
            bool(schedule_audit(pk)) for pk in queryset.exclude(status="removed").values_list("pk", flat=True)
        )
        self.message_user(
            request,
            f"已为 {count} 条申请安排重新机审，结果更新前保留当前收录状态。" if count else "没有可重新机审的申请，已移除的申请不能重新机审。",
            messages.SUCCESS if count else messages.WARNING,
        )


@admin.register(Repository)
class RepositoryAdmin(EvidenceAdmin):
    list_display = ("full_name", "platform", "stars", "archived", "fork", "synced_at")
    list_filter = ("platform", "archived", "fork")
    search_fields = ("full_name", "description", "platform_id")
    readonly_fields = ("repository_link",)
    fieldsets = (
        ("仓库信息", {"fields": ("full_name", "platform", "description", "repository_link", "stars", "archived", "fork")}),
        ("同步信息", {"fields": ("default_branch", "head_sha", "commit_at", "synced_at", "upstream_url", "upstream_name")}),
        ("平台标识", {"fields": ("platform_id", "owner_id", "name", "clone_url"), "classes": ("collapse",)}),
    )

    @admin.display(description="仓库主页")
    def repository_link(self, obj):
        return format_html('<a href="{}" target="_blank" rel="noopener noreferrer">{}</a>', obj.html_url, obj.html_url)


@admin.register(AuditResult)
class AuditResultAdmin(EvidenceAdmin):
    list_display = ("submission", "generation", "decision_label", "detector_version", "created_at")
    list_select_related = ("submission__repository", "submission__user")
    search_fields = ("submission__repository__full_name", "submission__user__username", "source_sha", "policy_sha")
    list_filter = ("created_at",)
    fields = ("submission", "generation", "decision_label", "source_sha", "policy_sha", "detector_version", "evidence_view", "created_at")
    readonly_fields = ("decision_label", "evidence_view")

    @admin.display(description="审核结论", ordering="decision")
    def decision_label(self, obj):
        return dict(Submission.Status.choices).get(obj.decision, obj.decision)

    @admin.display(description="审核证据")
    def evidence_view(self, obj):
        return formatted_json(obj.evidence)


@admin.register(AuditDispatch)
class AuditDispatchAdmin(EvidenceAdmin):
    list_display = ("submission", "generation", "dispatch_status", "created_at", "dispatched_at", "completed_at")
    list_select_related = ("submission__repository", "submission__user")
    search_fields = ("submission__repository__full_name", "submission__user__username")
    list_filter = ("completed_at", "dispatched_at")

    @admin.display(description="执行状态")
    def dispatch_status(self, obj):
        return "已完成" if obj.completed_at else "已派发" if obj.dispatched_at else "等待派发"


@admin.register(LicensePolicy)
class LicensePolicyAdmin(EvidenceAdmin):
    list_display = ("source_sha", "active", "detector_version", "created_at")
    list_filter = ("active",)
    search_fields = ("source_sha", "detector_version")
    fields = ("source_sha", "active", "detector_version", "manifest_view", "created_at")
    readonly_fields = ("manifest_view",)

    @admin.display(description="策略清单")
    def manifest_view(self, obj):
        return formatted_json(obj.manifest)


@admin.register(StarSnapshot)
class StarSnapshotAdmin(EvidenceAdmin):
    list_display = ("submission", "day", "stars", "observed_at")
    list_select_related = ("submission__repository", "submission__user")
    search_fields = ("submission__repository__full_name", "submission__user__username")
    date_hierarchy = "day"
