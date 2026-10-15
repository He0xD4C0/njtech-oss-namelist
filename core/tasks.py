import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from .models import PolicyDocument, SourceSyncState, SyncRun
from .runtime import get_setting


def begin_sync(kind):
    with transaction.atomic():
        state, _ = SourceSyncState.objects.get_or_create(kind=kind)
        state = SourceSyncState.objects.select_for_update().get(pk=state.pk)
        state.generation += 1
        state.save(update_fields=["generation"])
        return state.generation


def record_failure(kind):
    # Deliberately avoid exception strings: upstream URLs and credentials may appear in them.
    SyncRun.objects.create(
        kind=kind,
        status="failed",
        message="同步失败，保留最近有效内容 / Sync failed; last valid content retained",
    )
    notify_failure.delay(kind)


@shared_task(
    bind=True, autoretry_for=(ConnectionError, subprocess.SubprocessError), retry_backoff=True, max_retries=4
)
def sync_documents(self):
    from .git import fetch_snapshot

    source = get_setting("policy_source_url")
    if not source:
        return "not-configured"
    generation = begin_sync("documents")
    try:
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
        with transaction.atomic():
            state = SourceSyncState.objects.select_for_update().get(kind="documents")
            if state.generation != generation or get_setting("policy_source_url") != source:
                return "superseded"
            for language, documents in valid.items():
                for kind, body in documents.items():
                    existing = PolicyDocument.objects.filter(language=language, kind=kind).first()
                    if not existing or existing.body != body:
                        PolicyDocument.objects.update_or_create(
                            language=language, kind=kind, defaults={"body": body, "source_sha": snapshot.sha}
                        )
            PolicyDocument.objects.exclude(language__in=valid).delete()
            SyncRun.objects.create(kind="documents", status="success", source_sha=snapshot.sha)
        return snapshot.sha
    except Exception:
        record_failure("documents")
        raise


@shared_task(
    bind=True, autoretry_for=(ConnectionError, subprocess.SubprocessError), retry_backoff=True, max_retries=4
)
def sync_license_policy(self):
    from licensecheck.engine import (
        PolicyArtifactError,
        compile_policy,
        detector_version,
        validate_policy_artifacts,
    )
    from repositories.models import LicensePolicy, Submission
    from repositories.tasks import schedule_audit

    from .git import fetch_snapshot

    source = get_setting("license_source_url")
    if not source:
        return "not-configured"
    generation = begin_sync("licenses")
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
        with transaction.atomic():
            state = SourceSyncState.objects.select_for_update().get(kind="licenses")
            if state.generation != generation or get_setting("license_source_url") != source:
                return "superseded"
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
            SyncRun.objects.create(kind="licenses", status="success", source_sha=snapshot.sha)
            ids = list(Submission.objects.exclude(status="removed").values_list("pk", flat=True))
            for pk in ids:
                schedule_audit(pk)
        return snapshot.sha
    except Exception:
        record_failure("licenses")
        raise
    finally:
        if temporary and temporary.exists():
            shutil.rmtree(temporary)


@shared_task
def daily_source_sync():
    sync_documents.delay()
    sync_license_policy.delay()


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
        send_mail(
            "[OSS Directory] Task failure",
            f"任务 {kind} 失败，请查看管理面板。 / Task {kind} failed; review the administration panel.",
            None,
            recipients,
        )
    except Exception:
        SyncRun.objects.create(kind="alert", status="failed", message="告警邮件发送失败 / Alert email failed")
        return "mail-failed"
    return "sent"


@shared_task
def restore_drill():
    schedule = get_setting("restore_schedule", "monthly")
    if schedule == "off":
        return "disabled"
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
