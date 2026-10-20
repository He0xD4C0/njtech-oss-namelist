from django import forms
from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import Announcement


class AnnouncementForm(forms.ModelForm):
    class Meta:
        model = Announcement
        fields = ("title", "body", "kind", "include_future_users", "expires_at")
        widgets = {"body": forms.Textarea(attrs={"data-markdown-editor": "true", "rows": 18})}


@admin.register(Announcement)
class AnnouncementAdmin(ModelAdmin):
    form = AnnouncementForm
    list_display = ("title", "kind", "creator_name", "created_at", "expires_at", "revision")
    list_filter = ("kind",)
    search_fields = ("title", "body")
    readonly_fields = ("creator_name", "contributors", "revision", "created_at", "updated_at")
    fieldsets = (
        ("公告正文", {"fields": ("title", "body")}),
        ("展示规则", {"fields": ("kind", "include_future_users", "expires_at")}),
        ("编辑信息", {"fields": ("creator_name", "contributors", "revision", "created_at", "updated_at")}),
    )
    actions = None
    list_per_page = 50
    warn_unsaved_form = True

    @admin.display(description="所有编辑者")
    def contributors(self, obj):
        return ", ".join(item["name"] for item in obj.editors) if obj else ""

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_staff

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if not change:
            obj.creator = request.user
            obj.creator_name = request.user.get_full_name()
        obj.record_editor(request.user)
        super().save_model(request, obj, form, change)

    # Django's automatic LogEntry/history would violate the no-edit-history contract.
    def log_addition(self, request, obj, message):
        pass

    def log_change(self, request, obj, message):
        pass

    def history_view(self, request, object_id, extra_context=None):
        from django.http import Http404

        raise Http404("Announcement edit history is not retained")

    class Media:
        css = {"all": ("vendor/fontawesome/css/font-awesome.min.css", "vendor/easymde/easymde.min.css")}
        js = ("vendor/easymde/easymde.min.js", "js/markdown-editor.js")
