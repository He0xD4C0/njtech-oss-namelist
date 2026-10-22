import uuid

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models

username_validator = RegexValidator(r"^[a-z0-9_-]{3,32}$", "使用 3–32 位小写字母、数字、连字符或下划线。")


class User(AbstractUser):
    username = models.CharField(verbose_name="用户名", max_length=32, unique=True, validators=[username_validator])
    # Compatibility copy of the verified primary EmailAddress, never an identity.
    email = models.EmailField(verbose_name="默认邮箱", blank=True)
    full_name = models.CharField(verbose_name="姓名", max_length=150, blank=True)
    public_email = models.BooleanField(verbose_name="公开默认邮箱", default=False)
    language = models.CharField(
        verbose_name="语言", max_length=16, blank=True, choices=[("zh-hans", "简体中文"), ("en", "English")]
    )
    quota = models.PositiveIntegerField(verbose_name="仓库配额", default=64)
    avatar_key = models.CharField(verbose_name="头像存储路径", max_length=300, blank=True)
    avatar_revision = models.PositiveIntegerField(verbose_name="头像版本", default=0)

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
        verbose_name = "用户"
        verbose_name_plural = verbose_name
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(username="admin", is_superuser=True, is_staff=True, is_active=True)
                           | (~models.Q(username="admin") & models.Q(is_superuser=False))),
                name="fixed_admin_identity",
            ),
        ]


class RuntimeSetting(models.Model):
    key = models.CharField(verbose_name="配置项", max_length=80, unique=True)
    value = models.TextField(verbose_name="配置值", blank=True)
    secret = models.BooleanField(verbose_name="敏感配置", default=False)
    updated_at = models.DateTimeField(verbose_name="更新时间", auto_now=True)
    updated_by = models.ForeignKey(User, verbose_name="更新人", null=True, on_delete=models.SET_NULL)

    def __str__(self):
        return self.key

    class Meta:
        verbose_name = "运行时配置"
        verbose_name_plural = verbose_name


class SiteState(models.Model):
    id = models.PositiveSmallIntegerField(verbose_name="编号", primary_key=True, default=1, editable=False)
    activated_at = models.DateTimeField(verbose_name="激活时间", null=True, blank=True)
    bootstrap_user = models.ForeignKey(User, verbose_name="初始管理员", null=True, blank=True, on_delete=models.SET_NULL)

    def __str__(self):
        return "站点状态"

    class Meta:
        verbose_name = "站点状态"
        verbose_name_plural = verbose_name


class PolicyDocument(models.Model):
    kind = models.CharField(
        verbose_name="类型", max_length=16, choices=[("privacy", "Privacy"), ("terms", "Terms"), ("inclusion", "Inclusion")]
    )
    language = models.CharField(verbose_name="语言", max_length=16)
    body = models.TextField(verbose_name="正文")
    source_sha = models.CharField(verbose_name="来源版本", max_length=64)
    updated_at = models.DateTimeField(verbose_name="更新时间", auto_now=True)

    def __str__(self):
        kind = {"privacy": "隐私政策", "terms": "使用条款", "inclusion": "收录规则"}.get(self.kind, self.kind)
        return f"{kind} · {self.language}"

    class Meta:
        verbose_name = "政策正文"
        verbose_name_plural = verbose_name
        constraints = [models.UniqueConstraint(fields=["kind", "language"], name="unique_policy_language")]


class PolicyConsent(models.Model):
    user = models.ForeignKey(User, verbose_name="用户", on_delete=models.CASCADE)
    language = models.CharField(verbose_name="语言", max_length=16)
    versions = models.JSONField(verbose_name="同意的政策版本")
    created_at = models.DateTimeField(verbose_name="创建时间", auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} · 政策同意记录 #{self.pk}"

    class Meta:
        verbose_name = "政策同意记录"
        verbose_name_plural = verbose_name


class AuditEvent(models.Model):
    actor = models.ForeignKey(User, verbose_name="操作人", null=True, on_delete=models.SET_NULL)
    action = models.CharField(verbose_name="操作", max_length=100)
    subject = models.CharField(verbose_name="对象", max_length=200, blank=True)
    detail = models.JSONField(verbose_name="详情", default=dict)
    created_at = models.DateTimeField(verbose_name="创建时间", auto_now_add=True)

    def __str__(self):
        return f"操作记录 #{self.pk} · {self.subject}"

    class Meta:
        verbose_name = "配置操作记录"
        verbose_name_plural = verbose_name


class SyncRun(models.Model):
    kind = models.CharField(verbose_name="类型", max_length=32)
    status = models.CharField(verbose_name="状态", max_length=16)
    source_sha = models.CharField(verbose_name="来源版本", max_length=64, blank=True)
    message = models.TextField(verbose_name="结果说明", blank=True)
    detail = models.JSONField("运行摘要", default=dict, blank=True)
    attempts = models.PositiveIntegerField("执行尝试次数", default=0)
    created_at = models.DateTimeField(verbose_name="创建时间", auto_now_add=True)
    execution_token = models.UUIDField(verbose_name="执行标识", null=True, blank=True, editable=False)
    lease_expires_at = models.DateTimeField(verbose_name="执行权到期时间", null=True, blank=True, editable=False)

    def __str__(self):
        return f"任务 #{self.pk}"

    class Meta:
        verbose_name = "同步与备份任务"
        verbose_name_plural = verbose_name


class SourceSyncState(models.Model):
    kind = models.CharField(max_length=32, unique=True)
    generation = models.PositiveIntegerField(default=0)


class Incident(models.Model):
    key = models.CharField("故障标识", max_length=200)
    kind = models.CharField("类型", max_length=40)
    summary = models.TextField("脱敏原因")
    affected = models.JSONField("受影响对象", default=list)
    opened_at = models.DateTimeField("首次发生时间", auto_now_add=True)
    last_seen_at = models.DateTimeField("最近发生时间", auto_now=True)
    resolved_at = models.DateTimeField("恢复时间", null=True, blank=True)

    class Meta:
        verbose_name = "故障事件"
        verbose_name_plural = verbose_name
        constraints = [models.UniqueConstraint(fields=["key"], condition=models.Q(resolved_at=None), name="one_open_incident")]


class NotificationDelivery(models.Model):
    key = models.CharField("通知标识", max_length=200, unique=True)
    kind = models.CharField("通知类型", max_length=16)
    incidents = models.ManyToManyField(Incident, verbose_name="相关故障")
    status = models.CharField("状态", max_length=16, default="pending")
    attempts = models.PositiveIntegerField("尝试次数", default=0)
    reason = models.CharField("投递说明", max_length=200, blank=True)
    retry_at = models.DateTimeField("下次投递", null=True, blank=True)
    execution_token = models.UUIDField(null=True, editable=False)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    sent_at = models.DateTimeField("发送时间", null=True, blank=True)

    class Meta:
        verbose_name = "故障通知投递"
        verbose_name_plural = verbose_name


class PendingAvatarDeletion(models.Model):
    key = models.CharField(verbose_name="配置项", max_length=300, unique=True)
    created_at = models.DateTimeField(verbose_name="创建时间", auto_now_add=True)

    def __str__(self):
        return self.key

    class Meta:
        verbose_name = "待清理头像"
        verbose_name_plural = verbose_name


class BackupConfiguration(models.Model):
    """One encrypted candidate; independent files remain authoritative for active backups."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    revision = models.UUIDField(default=uuid.uuid4, editable=False)
    values = models.JSONField(default=dict)
    tested_revision = models.UUIDField(null=True, editable=False)
    applied_revision = models.UUIDField(null=True, editable=False)
    location = models.JSONField(default=dict)
