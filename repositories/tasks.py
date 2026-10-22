import subprocess
import uuid
from datetime import timedelta
from pathlib import Path

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from core.jobs import JobLease
from core.sync import OwnershipLost

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
        if not job or job.completed_at or job.status not in {"queued", "retrying", "running"}:
            return False
        if job.submission.status == "removed" or job.submission.generation != job.generation:
            job.completed_at = timezone.now()
            job.status = "superseded"
            job.save(update_fields=["completed_at", "status"])
            return False
        now = timezone.now()
        if job.retry_at and job.retry_at > now:
            return False
        if job.lease_expires_at and job.lease_expires_at > now:
            return False
        try:
            audit_submission.delay(job.submission_id, job.generation)
        except Exception:
            # The request is durable in PostgreSQL. A later reconciler retries it;
            # do not turn a successful committed submission into an HTTP error.
            job.reason_code, job.reason = "delivery", "任务服务暂不可用，等待自动补投递。"
            job.save(update_fields=["reason_code", "reason"])
            return False
        job.dispatched_at = timezone.now()
        job.status, job.execution_token = "queued", None
        if job.reason_code == "delivery":
            job.reason_code = job.reason = ""
        job.lease_expires_at = timezone.now() + timedelta(seconds=JobLease.seconds)
        job.save(update_fields=["dispatched_at", "lease_expires_at", "status", "execution_token", "reason", "reason_code"])
        return True


def schedule_audit(submission_id, *, expected_generation=None, lease=None):
    with transaction.atomic():
        submission = Submission.objects.select_for_update().filter(pk=submission_id).first()
        if not submission or submission.status == "removed":
            return False
        if expected_generation is not None and submission.generation != expected_generation:
            return False
        if lease:
            with lease.publish():
                pass
        submission.generation += 1
        submission.save(update_fields=["generation", "changed_at"])
        create_audit_dispatch(submission)
    return True


@shared_task(bind=True, max_retries=6)
def audit_submission(self, submission_id, generation):
    from core.incidents import provider_key, report, report_provider, resolve
    from providers.api import ProviderUnavailable
    from providers.models import OAuthBinding

    if not Submission.objects.filter(pk=submission_id, generation=generation).exclude(status="removed").exists():
        AuditDispatch.objects.filter(submission_id=submission_id, generation=generation).update(
            status="superseded", completed_at=timezone.now(), execution_token=None, lease_expires_at=None,
        )
        return "superseded"
    with transaction.atomic():
        job, _ = AuditDispatch.objects.get_or_create(submission_id=submission_id, generation=generation)
        job = AuditDispatch.objects.select_for_update().get(pk=job.pk)
        now = timezone.now()
        if job.completed_at or job.status in {"waiting_auth", "failed"}:
            return "already-completed" if job.status == "completed" else job.status
        if job.status == "running" and job.lease_expires_at and job.lease_expires_at > now:
            return "running"
        if job.retry_at and job.retry_at > now:
            return "retrying"
        if job.attempts >= 7:
            job.status, job.completed_at = "failed", now
            job.reason = "执行进程反复中断，已达到重试上限。"
            job.save()
            report(f"audit:{submission_id}", "audit", job.reason, [job.submission.repository.full_name])
            return "failed"
        job.status, job.execution_token = "running", uuid.uuid4()
        job.lease_expires_at = now + timedelta(seconds=JobLease.seconds)
        job.attempts += 1
        job.save()
    binding = OAuthBinding.objects.filter(user_id=job.submission.user_id, platform=job.submission.repository.platform).first()
    lease = JobLease(job)
    with lease.maintain():
        try:
            result = execute_audit(self, submission_id, generation, lease=lease)
            lease.finish(status="superseded" if result.startswith("superseded") else "completed",
                         completed_at=timezone.now(), reason="", reason_code="", retry_at=None)
            if result in {"published", "rejected", "historical", "needs_review", "unchanged", "ineligible"}:
                resolve(f"audit:{submission_id}")
                if binding:
                    resolve(provider_key(binding))
            return result
        except OwnershipLost:
            return "ownership-lost"
        except Exception as error:
            provider = isinstance(error, ProviderUnavailable)
            code = error.code if provider else "execution"
            retryable = error.retryable if provider else isinstance(error, (subprocess.SubprocessError, ConnectionError, OSError))
            status = "waiting_auth" if code == "reauthorization" else "retrying" if retryable and job.attempts < 7 else "failed"
            reason = {
                "reauthorization": "等待账号重新授权；最近许可证结论保持不变。",
                "configuration": "平台应用或加密配置异常，需要管理员修复。",
                "rate_limit": "平台限流，等待退避重试。",
            }.get(code, "平台、Git 或扫描执行异常。" if provider or retryable else "执行失败，请检查扫描器及运行配置。")
            if status == "failed" and retryable:
                reason += " 已达到重试上限，请修复后手动重新机审。"
            try:
                lease.finish(status=status, reason_code=code, reason=reason,
                             completed_at=timezone.now() if status == "failed" else None,
                             retry_at=timezone.now() + timedelta(seconds=max(min(3600, 60 * 2 ** (job.attempts - 1)), getattr(error, "retry_after", 0))) if status == "retrying" else None,
                             dispatched_at=None)
            except OwnershipLost:
                return "ownership-lost"
            if provider and binding:
                report_provider(binding, error)
            else:
                report(f"audit:{submission_id}", "audit", reason, [job.submission.repository.full_name])
            if not provider and not retryable:
                error.incident_recorded = True
                raise
            return status


def execute_audit(task, submission_id, generation, *, lease=None):
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
        raise ProviderUnavailable("平台账号未绑定。", code="reauthorization", retryable=False)
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
            if lease:
                with lease.publish():
                    pass
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
            schedule_audit(submission_id, expected_generation=generation, lease=lease)
            return "superseded-head"
        return apply_audit_result(
            submission_id,
            generation,
            snapshot.sha,
            policy,
            result,
            fresh.as_metadata(),
            snapshot.committed_at,
            snapshot=snapshot, lease=lease,
        )
    except RepositoryIneligible:
        mark_ineligible(
            submission_id,
            generation,
            "仓库已删除、转私有或不再归属该账号 / Repository deleted, private or no longer directly owned",
            lease=lease,
        )
        return "ineligible"


@shared_task
def recover_audits():
    """Repair delivery gaps and lost workers, never restart terminal failures."""
    for submission in Submission.objects.filter(status="queued").iterator():
        if not AuditDispatch.objects.filter(submission=submission, generation=submission.generation).exists():
            AuditDispatch.objects.get_or_create(submission=submission, generation=submission.generation)
    jobs = AuditDispatch.objects.filter(completed_at=None, status__in=["queued", "running", "retrying"])
    return sum(dispatch_audit(pk) for pk in jobs.values_list("pk", flat=True))


def resume_authorized(user_id, platform):
    """The account reconnect transaction calls this once after saving credentials."""
    ids = []
    with transaction.atomic():
        jobs = AuditDispatch.objects.select_for_update().filter(
            submission__user_id=user_id, submission__repository__platform=platform, status="waiting_auth",
        ).select_related("submission")
        for job in jobs:
            if job.submission.status == "removed" or job.generation != job.submission.generation:
                continue
            job.status, job.attempts, job.reason, job.reason_code = "queued", 0, "", ""
            job.dispatched_at = job.retry_at = job.lease_expires_at = job.execution_token = None
            job.save()
            ids.append(job.pk)
        for pk in ids:
            transaction.on_commit(lambda pk=pk: dispatch_audit(pk))
    return len(ids)


@shared_task
def sync_platforms():
    from core.incidents import provider_key, report_provider, resolve
    from core.models import SyncRun
    from providers.api import ProviderUnavailable, RepositoryIneligible, validate_repository
    from providers.models import OAuthBinding

    freeze_completed_months()
    failures, checked, binding_errors = [], 0, {}
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
            if binding.pk in binding_errors:
                raise binding_errors[binding.pk]
            data = validate_repository(binding, submission.repository.platform_id)
            resolve(provider_key(binding))
            checked += 1
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
        except ProviderUnavailable as error:
            binding_errors[binding.pk] = error
            report_provider(binding, error)
            failures.append({"repository": submission.repository.full_name, "code": error.code})
    SyncRun.objects.create(kind="provider-sync", status="failed" if failures else "success",
                           message=f"检查 {checked} 个仓库，失败 {len(failures)} 个。",
                           detail={"checked": checked, "failures": failures})


@shared_task
def sync_git():
    from core.git import fetch_snapshot
    from core.incidents import report, resolve
    from core.models import SyncRun

    failures, checked = [], 0
    for submission in Submission.objects.filter(status="published").select_related("repository"):
        try:
            snapshot = fetch_snapshot(
                submission.repository.clone_url, branch=submission.repository.default_branch
            )
            checked += 1
            resolve(f"git:{submission.pk}")
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
            failures.append({"repository": submission.repository.full_name, "code": "git_unavailable"})
            report(f"git:{submission.pk}", "git-sync", "公开 Git 抓取失败，保留最近有效内容。", [submission.repository.full_name])
    SyncRun.objects.create(kind="git-sync", status="failed" if failures else "success",
                           message=f"检查 {checked} 个仓库，失败 {len(failures)} 个。",
                           detail={"checked": checked, "failures": failures})


from .approvals import process_approval, recover_approvals  # noqa: E402,F401
from .originals import retrieve_originals  # noqa: E402,F401
