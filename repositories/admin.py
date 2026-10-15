from django.contrib import admin, messages
from unfold.admin import ModelAdmin

from .models import AuditDispatch, AuditResult, LicensePolicy, Repository, StarSnapshot, Submission


class EvidenceAdmin(ModelAdmin):
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
    list_display = ("repository", "user", "status", "audited_head", "changed_at")
    list_filter = ("status", "repository__platform", "repository__archived")
    search_fields = ("repository__full_name", "user__username")
    actions = ["recheck"]

    @admin.action(description="重新运行机审 / Re-run automated review", permissions=["view"])
    def recheck(self, request, queryset):
        from .tasks import schedule_audit

        count = sum(
            bool(schedule_audit(pk)) for pk in queryset.exclude(status="removed").values_list("pk", flat=True)
        )
        self.message_user(request, f"{count} queued", messages.SUCCESS)


for model in (Repository, AuditDispatch, AuditResult, LicensePolicy, StarSnapshot):
    admin.site.register(model, EvidenceAdmin)
