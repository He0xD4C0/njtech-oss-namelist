import uuid

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models

username_validator = RegexValidator(r"^[a-z0-9_-]{3,32}$", "使用 3–32 位小写字母、数字、连字符或下划线。")


class User(AbstractUser):
    username = models.CharField(max_length=32, unique=True, validators=[username_validator])
    # Compatibility copy of the verified primary EmailAddress, never an identity.
    email = models.EmailField(blank=True)
    full_name = models.CharField(max_length=150, blank=True)
    public_email = models.BooleanField(default=False)
    language = models.CharField(
        max_length=16, blank=True, choices=[("zh-hans", "简体中文"), ("en", "English")]
    )
    quota = models.PositiveIntegerField(default=64)
    avatar_key = models.CharField(max_length=300, blank=True)
    avatar_revision = models.PositiveIntegerField(default=0)

    def clean(self):
        super().clean()
        username_validator(self.username)
        self.email = self.email.lower()

    def get_full_name(self):
        return self.full_name or self.username

    @property
    def default_email(self):
        return self.emailaddress_set.filter(primary=True, verified=True).values_list("email", flat=True).first() or ""

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values("username").first()
            if original and original["username"] != self.username:
                raise ValidationError({"username": "用户名不可修改 / Username cannot be changed"})
        if (self.username == "admin") != bool(self.is_superuser) or (
            self.is_superuser and (not self.is_staff or not self.is_active)
        ):
            raise ValidationError("唯一超管必须为有效的 admin / The only superuser must be active admin")
        self.email = self.email.lower()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.username == "admin":
            raise ValidationError("不能注销超管 / The superuser cannot be deleted")
        return super().delete(*args, **kwargs)

    class Meta(AbstractUser.Meta):
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(username="admin", is_superuser=True, is_staff=True, is_active=True)
                           | (~models.Q(username="admin") & models.Q(is_superuser=False))),
                name="fixed_admin_identity",
            ),
        ]


class RuntimeSetting(models.Model):
    key = models.CharField(max_length=80, unique=True)
    value = models.TextField(blank=True)
    secret = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)


class SiteState(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    activated_at = models.DateTimeField(null=True, blank=True)
    bootstrap_user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)


class PolicyDocument(models.Model):
    kind = models.CharField(
        max_length=16, choices=[("privacy", "Privacy"), ("terms", "Terms"), ("inclusion", "Inclusion")]
    )
    language = models.CharField(max_length=16)
    body = models.TextField()
    source_sha = models.CharField(max_length=64)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["kind", "language"], name="unique_policy_language")]


class PolicyConsent(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    language = models.CharField(max_length=16)
    versions = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)


class AuditEvent(models.Model):
    actor = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=100)
    subject = models.CharField(max_length=200, blank=True)
    detail = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class SyncRun(models.Model):
    kind = models.CharField(max_length=32)
    status = models.CharField(max_length=16)
    source_sha = models.CharField(max_length=64, blank=True)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    execution_token = models.UUIDField(null=True, blank=True, editable=False)
    lease_expires_at = models.DateTimeField(null=True, blank=True, editable=False)


class SourceSyncState(models.Model):
    kind = models.CharField(max_length=32, unique=True)
    generation = models.PositiveIntegerField(default=0)


class PendingAvatarDeletion(models.Model):
    key = models.CharField(max_length=300, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)


class BackupConfiguration(models.Model):
    """One encrypted candidate; independent files remain authoritative for active backups."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    revision = models.UUIDField(default=uuid.uuid4, editable=False)
    values = models.JSONField(default=dict)
    tested_revision = models.UUIDField(null=True, editable=False)
    applied_revision = models.UUIDField(null=True, editable=False)
    location = models.JSONField(default=dict)
