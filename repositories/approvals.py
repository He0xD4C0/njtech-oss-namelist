"""Recoverable administrator approval: exact evidence → Git → policy → normal audits."""

import hashlib
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.git import validate_git_url
from core.jobs import JobLease
from core.models import AuditEvent, User
from core.runtime import get_setting
from core.sync import OwnershipLost, queue_sync

from .models import AuditDispatch, LicenseApproval, LicenseOriginal, LicensePolicy, Submission
from .policy_github import BranchMoved, PolicyGitHub, PolicyWriteError


def validate_original(original):
    if (not original.verified or original.error or not original.raw
            or original.audit.submission.status == "removed"):
        raise PolicyWriteError("已移除申请、无原文或原文未核验，不能审批。")
    raw = bytes(original.raw)
    if hashlib.sha256(raw).hexdigest() != original.sha256 or original.source_sha != original.audit.source_sha:
        raise PolicyWriteError("原文或固定来源校验失败，不能审批。")
    try:
        text = raw.decode("utf-8-sig")
        if not text.strip() or "\x00" in text:
            raise ValueError
    except (ValueError, UnicodeDecodeError):
        raise PolicyWriteError("原文编码无效，不能审批。") from None
    return text


def guard(job, lease=None):
    if not job.original_id:
        raise PolicyWriteError("原申请已删除，不能继续审批；已提交版本保留在记录中。")
    if not User.objects.filter(pk=job.actor_id, is_staff=True, is_active=True).exists():
        raise PolicyWriteError("审批管理员已失去权限，停止执行。")
    if validate_git_url(get_setting("license_source_url"), github_only=True) != job.target:
        raise PolicyWriteError("策略仓库配置已改变，停止执行；请按当前配置重新审批。")
    original = LicenseOriginal.objects.select_related("audit__submission").get(pk=job.original_id)
    validate_original(original)
    if lease:
        with lease.publish():
            pass
    return original


def enqueue(pk):
    with transaction.atomic():
        job = LicenseApproval.objects.select_for_update().get(pk=pk)
        if not job.active or job.status == "failed":
            return False
        now = timezone.now()
        if (job.lease_expires_at and job.lease_expires_at > now) or (job.retry_at and job.retry_at > now):
            return False
        try:
            process_approval.delay(pk)
        except Exception:
            return False
        job.lease_expires_at = now + timedelta(seconds=JobLease.seconds)
        job.status, job.execution_token = "queued", None
        job.save(update_fields=["lease_expires_at", "status", "execution_token"])
        return True


@transaction.atomic
def request_approval(original_id, actor):
    if not User.objects.filter(pk=actor.pk, is_staff=True, is_active=True).exists():
        raise PermissionDenied
    original = LicenseOriginal.objects.select_related("audit__submission").get(pk=original_id)
    validate_original(original)
    target = validate_git_url(get_setting("license_source_url"), github_only=True)
    # Serialize duplicate clicks for this immutable record; the unique target/digest
    # constraint also joins approvals of identical text from different repositories.
    LicenseOriginal.objects.select_for_update().get(pk=original_id)
    job, created = LicenseApproval.objects.get_or_create(
        digest=original.sha256, target=target, active=True, defaults={"original": original, "actor": actor},
    )
    if created:
        AuditEvent.objects.create(actor=actor, action="license.approval.requested", subject=str(job.pk),
                                  detail={"sha256": original.sha256, "target": target})
    transaction.on_commit(lambda: enqueue(job.pk))
    return job


@transaction.atomic
def retry_approval(pk, actor):
    if not User.objects.filter(pk=actor.pk, is_staff=True, is_active=True).exists():
        raise PermissionDenied
    job = LicenseApproval.objects.select_for_update().get(pk=pk)
    if job.status != "failed" or not job.active:
        return job
    job.actor_id = actor.pk
    guard(job)
    job.status, job.reason, job.attempts = "queued", "", 0
    job.retry_at = job.execution_token = job.lease_expires_at = None
    if job.stage == "sync":
        job.sync_run = None
    job.save()
    AuditEvent.objects.create(actor=actor, action="license.approval.retried", subject=str(pk), detail={"stage": job.stage})
    transaction.on_commit(lambda: enqueue(pk))
    return job


def progress(job, lease, stage=None, **values):
    if stage and stage != job.stage:
        stages = ["validate", "preflight", "commit", "sync", "reaudit", "done"]
        values["stage"] = stage
        if stages.index(stage) > stages.index(job.stage) and stage in {"sync", "reaudit", "done"}:
            values["attempts"] = 0
    with lease.publish() as current:
        for key, value in values.items():
            setattr(current, key, value)
            setattr(job, key, value)
        current.save()


def commit_policy(job, lease, original):
    from licensecheck.review import preflight

    client = PolicyGitHub(job.target, lambda: guard(job, lease))
    path = f"whitelist/reviewed-{original.sha256}.txt"
    for attempt in range(3):
        head = client.read()
        if client.contains(head, original):
            progress(job, lease, "sync", commit_sha=head.sha,
                     proposed_sha=job.proposed_sha or head.sha, branch=head.branch, reason="远端正文与来源哈希已确认，等待同步。")
            return
        progress(job, lease, "preflight", branch=head.branch)
        settings.WORK_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="review-", dir=settings.WORK_ROOT) as work:
            outcome = preflight(head.files, path, original.text, Path(work))
        if outcome == "allowed":
            progress(job, lease, "sync", commit_sha=head.sha, proposed_sha="", reason="当前策略已完整允许该正文，无需重复提交。")
            return
        progress(job, lease, "commit")
        guard(job, lease)
        sha = client.prepare_commit(head, original)
        progress(job, lease, proposed_sha=sha)
        try:
            client.advance(head.branch, sha)
        except BranchMoved:
            if attempt == 2:
                raise PolicyWriteError("分支连续三次前移；未覆盖他人提交，请稍后重试。") from None
            continue
        # An uncertain response is recovered by this same readback on the next run.
        confirmed = client.read()
        if not client.contains(confirmed, original):
            raise PolicyWriteError("提交后尚未确认远端正文与来源清单，等待回读。", retryable=True)
        progress(job, lease, "sync", commit_sha=sha, reason="仓库提交已回读确认，等待策略生效。")
        return


def synchronize(job, lease, original):
    from licensecheck.engine import scan_candidates

    from .tasks import schedule_audit

    guard(job, lease)
    policy = LicensePolicy.objects.filter(active=True).first()
    expected_path = f"whitelist/reviewed-{original.sha256}.txt"
    text_digest = hashlib.sha256(original.text.encode()).hexdigest()
    included = policy and (not job.proposed_sha or any(
        item.get("path") == expected_path and item.get("sha256") == text_digest
        for rule in policy.manifest.get("policies", {}).values() for item in rule.get("sources", [])
    ))
    if included and scan_candidates({"LICENSE": original.text}, policy.manifest, Path(policy.index_path))["decision"] == "published":
        with lease.publish():
            if not job.sync_run_id:
                schedule_audit(original.audit.submission_id)
            outcomes = {str(s.pk): {"generation": s.generation, "repository": s.repository.full_name}
                        for s in Submission.objects.exclude(status="removed").select_related("repository")}
            progress(job, lease, "reaudit", policy_sha=policy.source_sha, reaudit=outcomes, reason="策略已生效；正在核对仓库复审结果。")
        return True
    if job.sync_run_id and job.sync_run.status in {"failed", "queue-failed"}:
        raise PolicyWriteError("策略同步失败，上一版有效策略保留。修复后从同步阶段重试。")
    run, _ = queue_sync("licenses")
    progress(job, lease, sync_run=run)
    lease.finish(status="waiting", attempts=0, retry_at=timezone.now() + timedelta(seconds=30))
    return False


def check_reaudits(job, lease):
    outcomes, waiting, blocked = dict(job.reaudit), False, False
    for sid, outcome in outcomes.items():
        submission = Submission.objects.filter(pk=sid).first()
        if not submission or submission.status == "removed":
            outcome["status"] = "申请已移除"
            continue
        # Later policy/manual requests supersede old batches; follow the current
        # batch rather than declaring an abandoned historical task successful.
        outcome["generation"] = submission.generation
        dispatch = AuditDispatch.objects.filter(submission=submission, generation=submission.generation).first()
        audit = submission.audits.filter(generation=submission.generation).first()
        if audit:
            outcome["status"] = dict(Submission.Status.choices).get(audit.decision, audit.decision)
            outcome["policy_sha"] = audit.policy_sha
        elif dispatch and dispatch.status in {"waiting_auth", "failed"}:
            blocked = True
            outcome["status"] = dispatch.get_status_display()
            outcome["reason"] = dispatch.reason
        elif dispatch and dispatch.status == "completed":
            outcome["status"] = submission.get_status_display()
        else:
            waiting = True
            outcome["status"] = dispatch.get_status_display() if dispatch else "等待派发"
    progress(job, lease, reaudit=outcomes)
    if waiting or blocked:
        lease.finish(status="blocked" if blocked else "waiting", attempts=0,
                     reason="策略已生效，部分仓库复审受阻，请查看各仓库原因。" if blocked else "策略已生效，等待仓库复审。",
                     retry_at=timezone.now() + timedelta(minutes=1))
    else:
        lease.finish(stage="done", status="completed", active=False, reason="策略已生效，仓库复审已结束。", retry_at=None)


@shared_task
def recover_approvals():
    now = timezone.now()
    jobs = LicenseApproval.objects.filter(active=True).exclude(status="failed").filter(
        Q(lease_expires_at=None) | Q(lease_expires_at__lte=now), Q(retry_at=None) | Q(retry_at__lte=now),
    )
    return sum(enqueue(pk) for pk in jobs.values_list("pk", flat=True))


@shared_task
def process_approval(pk):
    from core.incidents import report, resolve
    from licensecheck.engine import PolicyCompilationError

    with transaction.atomic():
        job = LicenseApproval.objects.select_for_update().get(pk=pk)
        now = timezone.now()
        if not job.active or job.status == "failed":
            return job.status
        if job.status == "running" and job.lease_expires_at and job.lease_expires_at > now:
            return "running"
        if job.retry_at and job.retry_at > now:
            return "waiting"
        job.status, job.execution_token = "running", uuid.uuid4()
        job.lease_expires_at = now + timedelta(seconds=JobLease.seconds)
        job.attempts += 1
        job.save()
    lease = JobLease(job)
    with lease.maintain():
        try:
            original = guard(job, lease)
            if job.attempts > 6:
                raise PolicyWriteError("执行多次中断，已达到重试上限，请检查运行环境后重试本阶段。")
            if job.stage in {"validate", "preflight", "commit"}:
                commit_policy(job, lease, original)
            if job.stage == "sync" and not synchronize(job, lease, original):
                return "waiting"
            if job.stage == "reaudit":
                check_reaudits(job, lease)
            resolve(f"approval:{pk}")
            return "processed"
        except OwnershipLost:
            return "ownership-lost"
        except Exception as error:
            retry = isinstance(error, PolicyWriteError) and error.retryable and job.attempts < 6
            # Never expose arbitrary API/scanner exception output.
            reason = str(error) if isinstance(error, PolicyWriteError) else (
                "策略预编译失败或与黑名单冲突，未提交到仓库。" if isinstance(error, PolicyCompilationError)
                else "本阶段执行失败，请检查服务配置后重试；已提交的正文不会重复提交。"
            )
            try:
                lease.finish(status="waiting" if retry else "failed", reason=reason,
                             retry_at=timezone.now() + timedelta(seconds=max(min(3600, 60 * 2 ** job.attempts), getattr(error, "retry_after", 0))) if retry else None)
            except OwnershipLost:
                return "ownership-lost"
            report(f"approval:{pk}", "license-approval", reason, [job.target])
            return "waiting" if retry else "failed"
