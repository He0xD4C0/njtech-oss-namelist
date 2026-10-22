from django.contrib import admin, messages
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join
from unfold.admin import ModelAdmin

from core.admin import formatted_json
from core.i18n import management_message

from .models import (
    AuditDispatch,
    AuditResult,
    LicenseApproval,
    LicensePolicy,
    Repository,
    StarSnapshot,
    Submission,
)


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
    list_display = ("repository", "user", "status", "execution_reason", "license_conclusion", "original_link", "changed_at")
    list_select_related = ("repository", "user")
    list_filter = ("status", "repository__platform", "repository__archived")
    search_fields = ("repository__full_name", "user__username")
    actions = ["recheck"]
    search_help_text = "按仓库完整名称或申请人的用户名搜索。"
    fieldsets = (
        ("收录状态", {"fields": ("repository", "user", "status", "execution_reason", "reason_detail", "license_labels", "original_link", "changed_at")}),
        ("机审证据", {"fields": ("license_conclusion", "audited_head", "audited_policy", "audited_detector", "generation", "audit_history"),
                      "description": "请根据机审证据排查原因。许可证规则由独立策略仓库维护，同步后可重新运行机审。"}),
        ("收录与统计", {"fields": ("created_at", "published_at", "baseline_at", "baseline_stars"), "classes": ("collapse",)}),
    )
    readonly_fields = ("audit_history", "reason_detail", "execution_reason", "original_link", "license_conclusion")

    @admin.display(description="最近许可证结论")
    def license_conclusion(self, obj):
        result = obj.audits.order_by("-generation").first()
        if not result:
            return "尚无许可证机审结论"
        label = {"published": "完整允许", "needs_review": "待处理", "rejected": "未满足规则"}.get(result.decision, result.decision)
        suffix = "；当前批次尚无新结论" if result.generation != obj.generation else ""
        return f"第 {result.generation} 批：{label}{suffix}"

    @admin.display(description="当前执行状态与阻塞原因")
    def execution_reason(self, obj):
        job = obj.dispatches.filter(generation=obj.generation).first()
        return f"{job.get_status_display()}：{job.reason}" if job and job.reason else job.get_status_display() if job else "暂无任务"

    @admin.display(description="许可证原文")
    def original_link(self, obj):
        result = obj.audits.order_by("-generation").first()
        return originals_link(result) if result else "等待首次机审"

    @admin.display(description="处理原因")
    def reason_summary(self, obj):
        return self.reason_detail(obj)[:100]

    @admin.display(description="申请状态说明")
    def reason_detail(self, obj):
        return management_message(obj.reason) or "—"

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
    fields = ("submission", "generation", "decision_label", "original_link", "source_sha", "policy_sha", "detector_version", "evidence_view", "created_at")
    readonly_fields = ("decision_label", "evidence_view", "original_link")

    @admin.display(description="许可证原文与审批")
    def original_link(self, obj):
        return originals_link(obj)

    def get_urls(self):
        from . import review_views as views
        routes = [
            ("originals/<int:audit_id>/", views.originals, "license_originals"),
            ("originals/<int:audit_id>/inspect/", views.inspect, "license_inspect"),
            ("originals/<int:audit_id>/status/", views.inspection_status, "license_inspection_status"),
            ("original/<int:original_id>/approve/", views.approve, "license_approve"),
            ("original/<int:original_id>/recheck/", views.recheck, "license_recheck"),
            ("approval/<int:job_id>/", views.approval, "license_approval"),
            ("approval/<int:job_id>/status/", views.status, "license_approval_status"),
            ("approval/<int:job_id>/retry/", views.retry, "license_approval_retry"),
        ]
        return [path(route, self.admin_site.admin_view(view), name=name) for route, view, name in routes] + super().get_urls()

    @admin.display(description="审核结论", ordering="decision")
    def decision_label(self, obj):
        return dict(Submission.Status.choices).get(obj.decision, obj.decision)

    @admin.display(description="审核证据")
    def evidence_view(self, obj):
        return formatted_json(obj.evidence)


@admin.register(AuditDispatch)
class AuditDispatchAdmin(EvidenceAdmin):
    list_display = ("submission", "generation", "dispatch_status", "attempts", "reason", "retry_at", "lease_expires_at")
    list_select_related = ("submission__repository", "submission__user")
    search_fields = ("submission__repository__full_name", "submission__user__username")
    list_filter = ("completed_at", "dispatched_at")

    @admin.display(description="执行状态")
    def dispatch_status(self, obj):
        return obj.get_status_display()


def originals_link(audit):
    return format_html('<a href="{}">查看完整原文与审批</a>', reverse("admin:license_originals", args=[audit.pk]))


@admin.register(LicenseApproval)
class LicenseApprovalAdmin(EvidenceAdmin):
    list_display = ("pk", "actor", "stage", "reason", "progress_link", "updated_at")
    fields = ("original", "actor", "digest", "target", "stage", "reason", "commit_sha", "policy_sha", "progress_link", "created_at")
    readonly_fields = ("progress_link",)

    @admin.display(description="审批进度")
    def progress_link(self, obj):
        return format_html('<a href="{}">查看进度与恢复</a>', reverse("admin:license_approval", args=[obj.pk]))


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
