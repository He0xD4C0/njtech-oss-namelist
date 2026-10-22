from datetime import date, datetime

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from core.i18n import text
from core.models import AuditEvent, User

from .models import Repository, Submission


def freeze_completed_months():
    from directory.tasks import close_months

    close_months()


def json_metadata(metadata):
    return {
        key: value.isoformat() if isinstance(value, (date, datetime)) else value
        for key, value in metadata.items()
    }


def update_repository(repository, data):
    metadata = data.as_metadata()
    allowed = {field.name for field in Repository._meta.fields} - {"id", "platform", "platform_id"}
    for key, value in metadata.items():
        if key in allowed:
            setattr(repository, key, value)
    repository.synced_at = timezone.now()
    repository.save()
    return metadata


@transaction.atomic
def add_submission(user, platform="", stable_id="", *, repository_url=""):
    freeze_completed_months()
    from providers.api import get_client, parse_repository_url, validate_repository
    from providers.models import OAuthBinding

    user = User.objects.select_for_update().get(pk=user.pk)
    if user.submissions.exclude(status="removed").count() >= user.quota:
        raise ValueError(text(
            "已达到可提交的仓库数量上限。请先在下方移除不再展示的仓库，再提交新仓库。",
            "You have reached your repository limit. Remove an entry below before submitting another repository.",
        ))
    if repository_url:
        platform, path = parse_repository_url(repository_url)
    binding = OAuthBinding.objects.get(user=user, platform=platform)
    data = (get_client(platform, binding).repository_by_path(path) if repository_url
            else validate_repository(binding, str(stable_id)))
    stable_id = data.platform_id
    metadata = data.as_metadata()
    defaults = {key: value for key, value in metadata.items() if key not in {"platform", "platform_id"}}
    repository, _ = Repository.objects.get_or_create(
        platform=platform, platform_id=str(stable_id), defaults=defaults
    )
    # Audit/sync takes claim locks before the shared repository lock. Transfer
    # follows the same order, otherwise an old audit and new claim can deadlock.
    list(Submission.objects.select_for_update().filter(repository=repository).order_by("pk"))
    repository = Repository.objects.select_for_update().get(pk=repository.pk)
    if Submission.objects.filter(user=user, repository=repository).exclude(status="removed").exists():
        raise ValueError(text(
            "这个仓库已经提交过了。请在下方查看收录状态；修改仓库后可点击“重新检查”。",
            "This repository has already been submitted. Check its status below, or choose Recheck after updating it.",
        ))
    update_repository(repository, data)
    Submission.objects.filter(repository=repository).exclude(user=user).exclude(
        status__in=["removed", "historical"]
    ).update(
        status="historical",
        generation=F("generation") + 1,
        reason="归属已转移 / Ownership transferred",
        changed_at=timezone.now(),
    )
    submission = Submission.objects.create(user=user, repository=repository)
    AuditEvent.objects.create(actor=user, action="repository.submitted", subject=f"{platform}:{stable_id}")
    from .tasks import create_audit_dispatch

    create_audit_dispatch(submission)
    return submission


@transaction.atomic
def remove_submission(submission, actor):
    freeze_completed_months()
    # All mutation paths take user then claim locks, matching account unlink/delete.
    User.objects.select_for_update().get(pk=submission.user_id)
    current = Submission.objects.select_for_update().get(pk=submission.pk)
    current.status = Submission.Status.REMOVED
    current.generation += 1
    current.reason = "用户移除 / Removed by user"
    current.save(update_fields=["status", "generation", "reason", "changed_at"])
    AuditEvent.objects.create(
        actor=actor,
        action="repository.removed",
        subject=f"{current.repository.platform}:{current.repository.platform_id}",
    )


@transaction.atomic
def mark_ineligible(submission_id, generation, reason, *, lease=None):
    freeze_completed_months()
    submission = Submission.objects.select_for_update().filter(pk=submission_id).first()
    if not submission or submission.generation != generation or submission.status == "removed":
        return False
    if lease:
        with lease.publish():
            pass
    submission.status = "historical" if submission.published_at else "rejected"
    submission.reason = reason
    submission.generation += 1
    submission.save(update_fields=["status", "reason", "generation", "changed_at"])
    return True


@transaction.atomic
def apply_audit_result(submission_id, generation, source_sha, policy, result, metadata, committed_at, *, snapshot=None, lease=None):
    freeze_completed_months()
    # Source sync activation and audit publication share this lock. Stale policy work cannot win.
    from core.models import SourceSyncState
    from providers.models import OAuthBinding

    from .models import AuditResult, LicensePolicy

    SourceSyncState.objects.get_or_create(kind="licenses")
    SourceSyncState.objects.select_for_update().get(kind="licenses")
    submission = (
        Submission.objects.select_for_update().filter(pk=submission_id).select_related("repository").first()
    )
    if (
        not submission
        or submission.status == "removed"
        or submission.generation != generation
        or not LicensePolicy.objects.filter(
            pk=policy.pk, active=True, detector_version=policy.detector_version, manifest=policy.manifest
        ).exists()
        or not OAuthBinding.objects.filter(
            user_id=submission.user_id, platform=submission.repository.platform
        ).exists()
    ):
        return "superseded"
    if lease:
        with lease.publish():
            pass
    repository = Repository.objects.select_for_update().get(pk=submission.repository_id)
    if repository.head_sha and repository.head_sha != source_sha:
        return "superseded-head"
    if AuditResult.objects.filter(submission=submission, generation=generation).exists():
        return submission.status
    allowed = {field.name for field in Repository._meta.fields} - {"id", "platform", "platform_id"}
    for key, value in metadata.items():
        if key in allowed:
            setattr(repository, key, value)
    repository.head_sha = source_sha
    repository.commit_at = committed_at
    repository.synced_at = timezone.now()
    repository.save()
    audit = AuditResult.objects.create(
        submission=submission,
        generation=generation,
        source_sha=source_sha,
        policy_sha=policy.source_sha,
        detector_version=result["detector_version"],
        decision=result["decision"],
        evidence=result["evidence"],
    )
    if snapshot is not None:
        from .originals import save_originals
        save_originals(audit, snapshot, repository.clone_url)
    if result["decision"] == "published":
        now = timezone.now()
        if submission.status != "published":
            submission.baseline_at, submission.baseline_stars = now, metadata["stars"]
            submission.published_at = now
        submission.status = "published"
        metadata.update(head_sha=source_sha, commit_at=committed_at)
        submission.public_metadata = json_metadata(metadata)
        submission.license_labels = result["labels"]
        submission.reason = "白名单许可证验证通过 / Allowlisted license verified"
    else:
        submission.status = "historical" if submission.published_at else result["decision"]
        submission.reason = {
            "rejected": "许可证未满足收录规则 / License does not meet inclusion rules",
            "needs_review": "许可证需调整策略或源文件后重新机审 / License requires policy or source correction and recheck",
        }[result["decision"]]
    submission.audited_head, submission.audited_policy = source_sha, policy.source_sha
    submission.audited_detector = policy.detector_version
    submission.save()
    if lease:
        with lease.publish():
            pass
    return submission.status
