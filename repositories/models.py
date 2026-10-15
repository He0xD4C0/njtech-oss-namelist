from django.conf import settings
from django.db import models
from django.db.models import Q

PLATFORMS = [("github", "GitHub"), ("gitee", "Gitee"), ("gitlab", "GitLab.com")]


class Repository(models.Model):
    platform = models.CharField(max_length=12, choices=PLATFORMS)
    platform_id = models.CharField(max_length=100)
    name = models.CharField(max_length=300)
    full_name = models.CharField(max_length=400)
    description = models.TextField(blank=True)
    html_url = models.URLField(max_length=600)
    clone_url = models.URLField(max_length=600)
    owner_id = models.CharField(max_length=100)
    default_branch = models.CharField(max_length=300, default="main")
    fork = models.BooleanField(default=False)
    archived = models.BooleanField(default=False)
    upstream_url = models.URLField(max_length=600, blank=True)
    upstream_name = models.CharField(max_length=400, blank=True)
    stars = models.IntegerField(default=0)
    head_sha = models.CharField(max_length=64, blank=True)
    commit_at = models.DateTimeField(null=True, blank=True)
    synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
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

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="submissions")
    repository = models.ForeignKey(Repository, on_delete=models.PROTECT, related_name="submissions")
    status = models.CharField(max_length=16, choices=Status, default=Status.QUEUED)
    generation = models.PositiveIntegerField(default=1)
    reason = models.TextField(blank=True)
    public_metadata = models.JSONField(default=dict)
    license_labels = models.JSONField(default=list)
    audited_head = models.CharField(max_length=64, blank=True)
    audited_policy = models.CharField(max_length=64, blank=True)
    audited_detector = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)
    baseline_at = models.DateTimeField(null=True, blank=True)
    baseline_stars = models.IntegerField(null=True, blank=True)
    changed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "repository"],
                condition=~Q(status="removed"),
                name="one_retained_claim_per_user",
            )
        ]


class AuditResult(models.Model):
    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="audits")
    generation = models.PositiveIntegerField()
    source_sha = models.CharField(max_length=64)
    policy_sha = models.CharField(max_length=64)
    detector_version = models.CharField(max_length=100)
    decision = models.CharField(max_length=16)
    evidence = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["submission", "generation"], name="one_result_per_audit_generation"
            )
        ]


class AuditDispatch(models.Model):
    """Transactional outbox; broker availability cannot lose an audit request."""

    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="dispatches")
    generation = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["submission", "generation"], name="one_dispatch_per_audit_generation"
            )
        ]


class LicensePolicy(models.Model):
    source_sha = models.CharField(max_length=64, unique=True)
    detector_version = models.CharField(max_length=100)
    manifest = models.JSONField(default=dict)
    index_path = models.CharField(max_length=600)
    active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["active"], condition=Q(active=True), name="one_active_license_policy"
            )
        ]


class StarSnapshot(models.Model):
    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="snapshots")
    day = models.DateField()
    observed_at = models.DateTimeField()
    stars = models.IntegerField()
    baseline_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["submission", "day", "baseline_at"], name="daily_observation_per_claim_epoch"
            )
        ]
