from django.conf import settings
from django.db import models
from django.db.models import Q

PLATFORMS = [("github", "GitHub"), ("gitee", "Gitee"), ("gitlab", "GitLab.com")]


class Repository(models.Model):
    platform = models.CharField(verbose_name="平台", max_length=12, choices=PLATFORMS)
    platform_id = models.CharField(verbose_name="平台仓库编号", max_length=100)
    name = models.CharField(verbose_name="仓库名称", max_length=300)
    full_name = models.CharField(verbose_name="完整名称", max_length=400)
    description = models.TextField(verbose_name="简介", blank=True)
    html_url = models.URLField(verbose_name="仓库主页", max_length=600)
    clone_url = models.URLField(verbose_name="克隆地址", max_length=600)
    owner_id = models.CharField(verbose_name="平台所有者编号", max_length=100)
    default_branch = models.CharField(verbose_name="默认分支", max_length=300, default="main")
    fork = models.BooleanField(verbose_name="派生仓库", default=False)
    archived = models.BooleanField(verbose_name="已归档", default=False)
    upstream_url = models.URLField(verbose_name="上游地址", max_length=600, blank=True)
    upstream_name = models.CharField(verbose_name="上游仓库", max_length=400, blank=True)
    stars = models.IntegerField(verbose_name="星标数", default=0)
    head_sha = models.CharField(verbose_name="最新提交版本", max_length=64, blank=True)
    commit_at = models.DateTimeField(verbose_name="最近提交时间", null=True, blank=True)
    synced_at = models.DateTimeField(verbose_name="最近同步时间", null=True, blank=True)

    def __str__(self):
        return f"{self.get_platform_display()} · {self.full_name}"

    class Meta:
        verbose_name = "仓库"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(fields=["platform", "platform_id"], name="unique_provider_repository")
        ]


class Submission(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "待机审"
        NEEDS_REVIEW = "needs_review", "待处理"
        PUBLISHED = "published", "公开"
        REJECTED = "rejected", "打回"
        HISTORICAL = "historical", "历史"
        REMOVED = "removed", "已移除"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name="用户", on_delete=models.CASCADE, related_name="submissions")
    repository = models.ForeignKey(Repository, verbose_name="仓库", on_delete=models.PROTECT, related_name="submissions")
    status = models.CharField(verbose_name="状态", max_length=16, choices=Status, default=Status.QUEUED)
    generation = models.PositiveIntegerField(verbose_name="审核批次", default=1)
    reason = models.TextField(verbose_name="处理原因", blank=True)
    public_metadata = models.JSONField(verbose_name="公开信息", default=dict)
    license_labels = models.JSONField(verbose_name="许可证", default=list)
    audited_head = models.CharField(verbose_name="已审核提交版本", max_length=64, blank=True)
    audited_policy = models.CharField(verbose_name="已审核策略版本", max_length=64, blank=True)
    audited_detector = models.CharField(verbose_name="检测器版本", max_length=100, blank=True)
    created_at = models.DateTimeField(verbose_name="创建时间", auto_now_add=True)
    published_at = models.DateTimeField(verbose_name="公开时间", null=True, blank=True)
    baseline_at = models.DateTimeField(verbose_name="统计基线时间", null=True, blank=True)
    baseline_stars = models.IntegerField(verbose_name="基线星标数", null=True, blank=True)
    changed_at = models.DateTimeField(verbose_name="状态更新时间", auto_now=True)

    def __str__(self):
        return f"{self.repository.full_name} · {self.user.username} · {self.get_status_display()}"

    class Meta:
        verbose_name = "收录申请"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=["user", "repository"],
                condition=~Q(status="removed"),
                name="one_retained_claim_per_user",
            )
        ]


class AuditResult(models.Model):
    submission = models.ForeignKey(Submission, verbose_name="收录申请", on_delete=models.CASCADE, related_name="audits")
    generation = models.PositiveIntegerField(verbose_name="审核批次")
    source_sha = models.CharField(verbose_name="来源版本", max_length=64)
    policy_sha = models.CharField(verbose_name="策略版本", max_length=64)
    detector_version = models.CharField(verbose_name="检测器版本", max_length=100)
    decision = models.CharField(verbose_name="审核结论", max_length=16)
    evidence = models.JSONField(verbose_name="审核证据", default=dict)
    created_at = models.DateTimeField(verbose_name="创建时间", auto_now_add=True)

    def __str__(self):
        return f"{self.submission.repository.full_name} · 第 {self.generation} 批机审结果"

    class Meta:
        verbose_name = "机审结果"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=["submission", "generation"], name="one_result_per_audit_generation"
            )
        ]


class AuditDispatch(models.Model):
    """Transactional outbox; broker availability cannot lose an audit request."""

    submission = models.ForeignKey(Submission, verbose_name="收录申请", on_delete=models.CASCADE, related_name="dispatches")
    generation = models.PositiveIntegerField(verbose_name="审核批次")
    created_at = models.DateTimeField(verbose_name="创建时间", auto_now_add=True)
    dispatched_at = models.DateTimeField(verbose_name="派发时间", null=True, blank=True)
    completed_at = models.DateTimeField(verbose_name="完成时间", null=True, blank=True)

    def __str__(self):
        return f"{self.submission.repository.full_name} · 第 {self.generation} 批机审任务"

    class Meta:
        verbose_name = "机审任务"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=["submission", "generation"], name="one_dispatch_per_audit_generation"
            )
        ]


class LicensePolicy(models.Model):
    source_sha = models.CharField(verbose_name="来源版本", max_length=64, unique=True)
    detector_version = models.CharField(verbose_name="检测器版本", max_length=100)
    manifest = models.JSONField(verbose_name="策略清单", default=dict)
    index_path = models.CharField(verbose_name="索引路径", max_length=600)
    active = models.BooleanField(verbose_name="当前生效", default=False)
    created_at = models.DateTimeField(verbose_name="创建时间", auto_now_add=True)

    def __str__(self):
        return f"许可证策略 · {self.source_sha[:12]}"

    class Meta:
        verbose_name = "许可证策略"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=["active"], condition=Q(active=True), name="one_active_license_policy"
            )
        ]


class StarSnapshot(models.Model):
    submission = models.ForeignKey(Submission, verbose_name="收录申请", on_delete=models.CASCADE, related_name="snapshots")
    day = models.DateField(verbose_name="统计日期")
    observed_at = models.DateTimeField(verbose_name="采样时间")
    stars = models.IntegerField(verbose_name="星标数")
    baseline_at = models.DateTimeField(verbose_name="统计基线时间")

    def __str__(self):
        return f"{self.submission.repository.full_name} · {self.day} · {self.stars}"

    class Meta:
        verbose_name = "星标记录"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=["submission", "day", "baseline_at"], name="daily_observation_per_claim_epoch"
            )
        ]
