import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .i18n import text
from .mail import send_system_mail
from .models import PolicyDocument, SyncRun
from .runtime import get_setting
from .sync import execute_sync


def record_failure(kind):
    # Deliberately avoid exception strings: upstream URLs and credentials may appear in them.
    SyncRun.objects.create(
        kind=kind,
        status="failed",
        message="同步失败，保留最近有效内容 / Sync failed; last valid content retained",
    )
    notify_failure.delay(kind)


@shared_task(bind=True, max_retries=None)
def sync_documents(self, run_id=None, source=None, generation=None, business_retries=0):
    return execute_sync(self, "documents", _sync_documents, run_id, source, generation, business_retries)


def _sync_documents(lease, source, generation):
    from .git import fetch_snapshot

    snapshot = fetch_snapshot(source, mode="documents")
    valid = {
        language: {
            kind: snapshot.files.get(f"{language}/{kind}.md", "")
            for kind in ("privacy", "terms", "inclusion")
        }
        for language in ("zh-CN", "en-US")
    }
    valid = {
        language: documents
        for language, documents in valid.items()
        if all(body.strip() for body in documents.values())
    }
    if not valid:
        raise ValueError("No complete policy language")
    with lease.publish():
        for language, documents in valid.items():
            for kind, body in documents.items():
                existing = PolicyDocument.objects.filter(language=language, kind=kind).first()
                if not existing or existing.body != body:
                    PolicyDocument.objects.update_or_create(
                        language=language, kind=kind, defaults={"body": body, "source_sha": snapshot.sha}
                    )
        PolicyDocument.objects.exclude(language__in=valid).delete()
        lease.finish("success", snapshot.sha)
    return snapshot.sha


@shared_task(bind=True, max_retries=None)
def sync_license_policy(self, run_id=None, source=None, generation=None, business_retries=0):
    return execute_sync(self, "licenses", _sync_license_policy, run_id, source, generation, business_retries)


def _sync_license_policy(lease, source, generation):
    from licensecheck.engine import (
        PolicyArtifactError,
        compile_policy,
        detector_version,
        validate_policy_artifacts,
    )
    from repositories.models import LicensePolicy, Submission
    from repositories.tasks import schedule_audit

    from .git import fetch_snapshot

    temporary = None
    try:
        snapshot = fetch_snapshot(source, mode="rules")
        active = LicensePolicy.objects.filter(source_sha=snapshot.sha, active=True).first()
        if active and active.detector_version == detector_version():
            try:
                validate_policy_artifacts(active.manifest, Path(active.index_path))
            except PolicyArtifactError:
                pass  # Cache is disposable; rebuilding must also work after DB-only restoration.
            else:
                with lease.publish():
                    lease.finish("unchanged", snapshot.sha)
                return "unchanged"
        settings.WORK_ROOT.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="compile-", dir=settings.WORK_ROOT))
        manifest = compile_policy(snapshot.files, temporary)
        version = manifest["detector_version"]
        final = (
            settings.WORK_ROOT
            / "license-index"
            / f"{snapshot.sha}-{hashlib.sha256(version.encode()).hexdigest()[:12]}-{generation}"
        )
        final.parent.mkdir(parents=True, exist_ok=True)
        with lease.publish():
            if not final.exists():
                os.rename(temporary, final)
                temporary = None
            LicensePolicy.objects.filter(active=True).update(active=False)
            LicensePolicy.objects.update_or_create(
                source_sha=snapshot.sha,
                defaults={
                    "active": True,
                    "detector_version": version,
                    "manifest": manifest,
                    "index_path": str(final),
                },
            )
            ids = list(Submission.objects.exclude(status="removed").values_list("pk", flat=True))
            for pk in ids:
                schedule_audit(pk)
            lease.finish("success", snapshot.sha)
        return snapshot.sha
    finally:
        if temporary and temporary.exists():
            shutil.rmtree(temporary)


@shared_task
def daily_source_sync():
    from .sync import queue_sync

    for kind in ("documents", "licenses"):
        if get_setting("policy_source_url" if kind == "documents" else "license_source_url"):
            queue_sync(kind)


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=5)
def delete_avatar_object(self, key):
    from .avatar import storage
    from .models import PendingAvatarDeletion, User

    if User.objects.filter(avatar_key=key).exists():
        return "still-in-use"
    storage().delete(key)
    PendingAvatarDeletion.objects.filter(key=key).delete()
    return "deleted"


@shared_task
def recover_avatar_deletions():
    from .models import PendingAvatarDeletion

    count = 0
    for key in PendingAvatarDeletion.objects.values_list("key", flat=True).iterator():
        try:
            delete_avatar_object.delay(key)
        except Exception:
            break
        count += 1
    return count


@shared_task
def notify_failure(kind):
    recipients = [value.strip() for value in get_setting("operations_emails").split(",") if value.strip()]
    if not recipients:
        return "no-recipients"
    try:
        send_system_mail(
            text("NJTech OSS · 任务失败通知", "NJTech OSS · Task failure"),
            text(f"任务 {kind} 执行失败，请到管理后台查看记录并处理。", f"Task {kind} failed. Review the records in the administration panel."),
            recipients, action_url=settings.SITE_BASE_URL.rstrip("/") + "/manage/settings/",
            action_label=text("查看运行状态", "View status"),
        )
    except Exception:
        SyncRun.objects.create(kind="alert", status="failed", message="告警邮件发送失败 / Alert email failed")
        return "mail-failed"
    return "sent"


@shared_task
def report_backup_readiness():
    """Run in maintenance, the only application container with backup credentials."""
    from django.core.cache import cache

    from deploy.backup.readiness import readiness

    try:
        state = readiness()
    except (ValueError, OSError):
        state = "invalid"
    try:
        from deploy.backup import managed
        report = managed.status() if managed.enabled() else {}
        if managed.enabled():
            try:
                with managed.lock(blocking=False):
                    report["busy"] = False
                    # Reconcile publication interrupted before DB commit, under the same lock as apply.
                    if state == "ready":
                        from .models import BackupConfiguration
                        active = managed.active()
                        if active.get("location"):
                            BackupConfiguration.objects.filter(pk=1).update(
                                applied_revision=active["revision"], location=active["location"])
            except BlockingIOError:
                report["busy"] = True
            if report.get("state") == "running" and not report["busy"]:
                report["state"] = "interrupted"
        cache.set("backup-readiness", {"state": state, "backup": report, "checked_at": timezone.now().isoformat()}, timeout=180)
    except Exception:
        # Status reporting must not stop the independent backup scheduler.
        return "unavailable"
    return state


@shared_task
def restore_drill():
    schedule = get_setting("restore_schedule", "monthly")
    if schedule == "off":
        return "disabled"
    from deploy.backup.readiness import readiness

    try:
        state = readiness()
    except (ValueError, OSError):
        record_failure("restore")
        return "failed"
    if state != "ready":
        return state
    last = SyncRun.objects.filter(kind="restore", status="success").order_by("-created_at").first()
    from dateutil.relativedelta import relativedelta

    interval = {
        "weekly": relativedelta(weeks=1),
        "monthly": relativedelta(months=1),
        "quarterly": relativedelta(months=3),
    }.get(schedule)
    if interval is None:
        return "disabled"
    if last and timezone.now() < last.created_at + interval:
        return "not-due"
    from django.core.cache import cache

    if not cache.add("restore-drill-running", "1", timeout=21600):
        return "already-running"
    try:
        subprocess.run(
            ["/app/deploy/backup/restore-drill.sh"], check=True, timeout=18000, capture_output=True
        )
        SyncRun.objects.create(
            kind="restore",
            status="success",
            message="隔离恢复与配置解密验证通过 / Isolated restore and key verification passed",
        )
        return "success"
    except Exception:
        record_failure("restore")
        return "failed"
    finally:
        cache.delete("restore-drill-running")


# Register the backup task in Celery autodiscovery without exposing secrets to the broker.
from .backup import backup_operation  # noqa: E402,F401
