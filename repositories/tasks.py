import subprocess
from datetime import timedelta
from pathlib import Path

from celery import shared_task
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.tasks import notify_failure

from .models import AuditDispatch, LicensePolicy, Repository, Submission
from .services import (
    apply_audit_result,
    freeze_completed_months,
    json_metadata,
    mark_ineligible,
    update_repository,
)


def create_audit_dispatch(submission):
    job, _ = AuditDispatch.objects.get_or_create(submission=submission, generation=submission.generation)
    transaction.on_commit(lambda: dispatch_audit(job.pk))
    return job


def dispatch_audit(job_id):
    with transaction.atomic():
        job = AuditDispatch.objects.select_for_update().filter(pk=job_id).select_related("submission").first()
        if not job or job.completed_at:
            return False
        if job.submission.status == "removed" or job.submission.generation != job.generation:
            job.completed_at = timezone.now()
            job.save(update_fields=["completed_at"])
            return False
        try:
            audit_submission.delay(job.submission_id, job.generation)
        except Exception:
            # The request is durable in PostgreSQL. A later reconciler retries it;
            # do not turn a successful committed submission into an HTTP error.
            return False
        job.dispatched_at = timezone.now()
        job.save(update_fields=["dispatched_at"])
        return True


def schedule_audit(submission_id):
    with transaction.atomic():
        submission = Submission.objects.select_for_update().filter(pk=submission_id).first()
        if not submission or submission.status == "removed":
            return False
        submission.generation += 1
        submission.save(update_fields=["generation", "changed_at"])
        create_audit_dispatch(submission)
    return True


@shared_task(bind=True, max_retries=6)
def audit_submission(self, submission_id, generation):
    if (
        not Submission.objects.filter(pk=submission_id, generation=generation)
        .exclude(status="removed")
        .exists()
    ):
        AuditDispatch.objects.filter(submission_id=submission_id, generation=generation).update(
            completed_at=timezone.now()
        )
        return "superseded"
    if AuditDispatch.objects.filter(
        submission_id=submission_id, generation=generation, completed_at__isnull=False
    ).exists():
        return "already-completed"
    result = execute_audit(self, submission_id, generation)
    AuditDispatch.objects.filter(submission_id=submission_id, generation=generation).update(
        completed_at=timezone.now()
    )
    return result


def execute_audit(task, submission_id, generation):
    from core.git import GitLicenseInputError, fetch_snapshot
    from licensecheck.engine import CandidateValidationError, scan_candidates
    from providers.api import ProviderUnavailable, RepositoryIneligible, validate_repository
    from providers.models import OAuthBinding

    freeze_completed_months()
    submission = Submission.objects.filter(pk=submission_id).select_related("repository", "user").first()
    if not submission or submission.status == "removed" or submission.generation != generation:
        return "superseded"
    binding = OAuthBinding.objects.filter(
        user=submission.user, platform=submission.repository.platform
    ).first()
    if not binding:
        return "unlinked"
    policy = LicensePolicy.objects.filter(active=True).first()
    if not policy:
        if not submission.published_at:
            Submission.objects.filter(pk=submission.pk, generation=generation).exclude(
                status="removed"
            ).update(status="needs_review", reason="尚无有效许可证策略 / No active license policy")
        return "no-policy"
    try:
        data = validate_repository(binding, submission.repository.platform_id)
        metadata = data.as_metadata()
        invalid_input = None
        try:
            snapshot = fetch_snapshot(metadata["clone_url"], branch=metadata["default_branch"])
        except GitLicenseInputError as error:
            snapshot = error.snapshot
            invalid_input = error.evidence
        # Capture the freshly fetched head before scanning. Another observer can invalidate it.
        with transaction.atomic():
            current = Submission.objects.select_for_update().get(pk=submission.pk)
            if current.status == "removed" or current.generation != generation:
                return "superseded"
            repository = Repository.objects.select_for_update().get(pk=current.repository_id)
            update_repository(repository, data)
            repository.head_sha, repository.commit_at = snapshot.sha, snapshot.committed_at
            repository.save(update_fields=["head_sha", "commit_at"])
        if (
            submission.status == "published"
            and submission.audited_head == snapshot.sha
            and submission.audited_policy == policy.source_sha
            and submission.audited_detector == policy.detector_version
        ):
            return "unchanged"
        if invalid_input:
            result = {
                "decision": "needs_review",
                "labels": [],
                "evidence": invalid_input,
                "detector_version": policy.detector_version,
            }
        elif snapshot.decoding_errors:
            result = {
                "decision": "needs_review",
                "labels": [],
                "evidence": {
                    "reason": "unsupported_license_encoding",
                    "files": [
                        {"path": path, "sha256": digest, "decoding_error": "invalid_utf8_or_binary"}
                        for path, digest in snapshot.decoding_errors.items()
                    ],
                },
                "detector_version": policy.detector_version,
            }
        elif not snapshot.files:
            result = {
                "decision": "rejected",
                "labels": [],
                "evidence": {"reason": "no_license_candidates", "files": []},
                "detector_version": policy.detector_version,
            }
        else:
            try:
                result = scan_candidates(snapshot.files, policy.manifest, Path(policy.index_path))
            except CandidateValidationError:
                result = {
                    "decision": "needs_review",
                    "labels": [],
                    "evidence": {
                        "reason": "invalid_license_text",
                        "files": [{"path": path} for path in snapshot.files],
                    },
                    "detector_version": policy.detector_version,
                }
        # Recheck visibility/ownership after slow scans; never publish a newly-private repository.
        fresh = validate_repository(binding, submission.repository.platform_id)
        if fresh.head_sha and fresh.head_sha != snapshot.sha:
            schedule_audit(submission_id)
            return "superseded-head"
        return apply_audit_result(
            submission_id,
            generation,
            snapshot.sha,
            policy,
            result,
            fresh.as_metadata(),
            snapshot.committed_at,
        )
    except RepositoryIneligible:
        mark_ineligible(
            submission_id,
            generation,
            "仓库已删除、转私有或不再归属该账号 / Repository deleted, private or no longer directly owned",
        )
        return "ineligible"
    except (ProviderUnavailable, subprocess.SubprocessError, ConnectionError, OSError):
        if task.request.retries >= task.max_retries:
            notify_failure.delay("repository-audit")
        raise task.retry(
            exc=RuntimeError("Temporary provider, Git or scanner failure"),
            countdown=min(3600, 60 * 2**task.request.retries),
        ) from None


@shared_task
def recover_audits():
    """Repair commit-to-broker gaps and abandoned delivery after process loss."""
    for submission in Submission.objects.filter(status="queued").iterator():
        pending = AuditDispatch.objects.filter(
            submission=submission, generation=submission.generation, completed_at__isnull=True
        ).exists()
        if not pending:
            schedule_audit(submission.pk)
    cutoff = timezone.now() - timedelta(hours=3)
    jobs = AuditDispatch.objects.filter(completed_at__isnull=True).filter(
        Q(dispatched_at__isnull=True) | Q(dispatched_at__lt=cutoff)
    )
    return sum(dispatch_audit(pk) for pk in jobs.values_list("pk", flat=True))


@shared_task
def sync_platforms():
    from providers.api import ProviderUnavailable, RepositoryIneligible, validate_repository
    from providers.models import OAuthBinding

    freeze_completed_months()
    ids = list(
        Submission.objects.filter(status__in=["published", "queued", "needs_review"]).values_list(
            "pk", flat=True
        )
    )
    for pk in ids:
        submission = Submission.objects.select_related("repository", "user").filter(pk=pk).first()
        if not submission:
            continue
        generation = submission.generation
        binding = OAuthBinding.objects.filter(
            user=submission.user, platform=submission.repository.platform
        ).first()
        if not binding:
            continue
        try:
            data = validate_repository(binding, submission.repository.platform_id)
            with transaction.atomic():
                current = Submission.objects.select_for_update().get(pk=pk)
                if current.status == "removed" or current.generation != generation:
                    continue
                repository = Repository.objects.select_for_update().get(pk=current.repository_id)
                metadata = update_repository(repository, data)
                if current.status == "published":
                    metadata.update(head_sha=repository.head_sha, commit_at=repository.commit_at)
                    current.public_metadata = json_metadata(metadata)
                    current.save(update_fields=["public_metadata", "changed_at"])
                    if not repository.head_sha:
                        transaction.on_commit(lambda pk=current.pk: schedule_audit(pk))
        except RepositoryIneligible:
            mark_ineligible(
                pk,
                generation,
                "平台验证不再满足公开或直接归属条件 / Provider verification no longer meets visibility or ownership requirements",
            )
        except ProviderUnavailable:
            notify_failure.delay("provider-sync")


@shared_task
def sync_git():
    from core.git import fetch_snapshot

    for submission in Submission.objects.filter(status="published").select_related("repository"):
        try:
            snapshot = fetch_snapshot(
                submission.repository.clone_url, branch=submission.repository.default_branch
            )
            if snapshot.sha != submission.audited_head:
                with transaction.atomic():
                    current = Submission.objects.select_for_update().get(pk=submission.pk)
                    if current.status != "published" or current.generation != submission.generation:
                        continue
                    Repository.objects.filter(pk=current.repository_id).update(
                        head_sha=snapshot.sha, commit_at=snapshot.committed_at
                    )
                    transaction.on_commit(lambda pk=current.pk: schedule_audit(pk))
        except (subprocess.SubprocessError, OSError, ValueError):
            notify_failure.delay("git-sync")
