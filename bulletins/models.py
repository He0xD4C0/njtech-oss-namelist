from django.conf import settings
from django.db import models, transaction
from django.utils import timezone


class Announcement(models.Model):
    class Kind(models.TextChoices):
        NORMAL = "normal", "普通公告"
        POLICY = "policy", "政策公告"

    title = models.CharField(max_length=200)
    body = models.TextField(help_text="Markdown。保存后立即应用，不保留旧正文。")
    kind = models.CharField(max_length=10, choices=Kind, default=Kind.NORMAL)
    include_future_users = models.BooleanField(
        default=True, help_text="关闭后仅面向创建时已存在的账号；截止时间不会随编辑改变。"
    )
    expires_at = models.DateTimeField(
        null=True, blank=True, help_text="留空表示永久有效；过期后仍保留公开正文。"
    )
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="created_announcements"
    )
    creator_name = models.CharField(max_length=150)
    editors = models.JSONField(default=list)
    revision = models.PositiveIntegerField(default=1, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return self.title

    @property
    def expired(self):
        return self.expires_at is not None and self.expires_at <= timezone.now()

    def targets(self, user):
        return (
            user.is_authenticated
            and user.is_active
            and (self.include_future_users or user.date_joined <= self.created_at)
        )

    def record_editor(self, user):
        contributor = {"id": user.pk, "name": user.get_full_name()}
        # A name is preserved as public attribution even if the account later disappears.
        if not any(item["id"] == user.pk for item in self.editors):
            self.editors = [*self.editors, contributor]

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                previous = type(self).objects.select_for_update().get(pk=self.pk)
                self.created_at = previous.created_at
                self.creator_id = previous.creator_id
                self.creator_name = previous.creator_name
                written_fields = kwargs.get("update_fields")
                content_changed = any(
                    (written_fields is None or field in written_fields)
                    and getattr(self, field) != getattr(previous, field)
                    for field in ("title", "body")
                )
                self.revision = previous.revision + int(content_changed)
                # Preserve contributions made by a concurrent edit; never store old bodies.
                known = {item["id"] for item in previous.editors}
                self.editors = previous.editors + [item for item in self.editors if item["id"] not in known]
                if kwargs.get("update_fields") is not None:
                    kwargs["update_fields"] = set(kwargs["update_fields"]) | {
                        "revision",
                        "editors",
                        "updated_at",
                    }
            super().save(*args, **kwargs)


class AnnouncementReceipt(models.Model):
    announcement = models.ForeignKey(Announcement, on_delete=models.CASCADE, related_name="receipts")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    delivered_at = models.DateTimeField(null=True, blank=True)
    confirmed_revision = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["announcement", "user"], name="one_announcement_receipt_per_user")
        ]
