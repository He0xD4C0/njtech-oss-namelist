"""Immutable license evidence. Historical retrieval always names the audited commit."""

import hashlib
import re
import subprocess
from datetime import timedelta
from pathlib import Path

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from core.git import fetch_snapshot

from .models import AuditResult, LicenseOriginal, LicensePolicy


def save_originals(audit, snapshot, source_url, *, historical=False):
    evidence = {item.get("path"): item for item in audit.evidence.get("files", [])}
    for path, content in snapshot.files.items():
        raw = snapshot.raw_files.get(path, content.encode("utf-8"))
        digest = hashlib.sha256(raw).hexdigest()
        error = ""
        try:
            decoded = raw.decode("utf-8-sig")
            if not decoded.strip() or "\x00" in decoded or path in snapshot.decoding_errors:
                raise ValueError
        except (UnicodeDecodeError, ValueError):
            error = "原文不是有效的 UTF-8 许可证正文，不能审批。"
        if historical:
            expected = evidence.get(path, {}).get("sha256")
            # Existing detector evidence hashes decoded UTF-8, while the new snapshot
            # records the original bytes (including an optional UTF-8 BOM).
            if not expected or expected not in {digest, hashlib.sha256(content.encode("utf-8")).hexdigest()}:
                error = "历史证据缺少哈希或原文哈希不符，不能审批。"
        LicenseOriginal.objects.get_or_create(audit=audit, path=path, defaults={
            "raw": raw, "sha256": digest, "source_url": source_url,
            "source_sha": snapshot.sha, "verified": not error, "error": error,
        })


@shared_task
def retrieve_originals(audit_id):
    audit = AuditResult.objects.select_related("submission__repository").get(pk=audit_id)
    if audit.originals.exists():
        return "stored"
    if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", audit.source_sha):
        AuditResult.objects.filter(pk=audit_id).update(original_error="历史记录缺少有效的固定提交，不能补取原文。")
        return "unavailable"
    source = audit.submission.repository.clone_url
    try:
        snapshot = fetch_snapshot(source, revision=audit.source_sha)
        if snapshot.sha != audit.source_sha:
            raise ValueError
    except (ValueError, subprocess.SubprocessError, OSError):
        # Keep immutable audit evidence untouched; only retrieval diagnostics change.
        AuditResult.objects.filter(pk=audit_id).update(original_error="无法取得机审时的固定提交原文，请稍后重试。")
        return "unavailable"
    with transaction.atomic():
        AuditResult.objects.select_for_update().get(pk=audit_id)
        save_originals(audit, snapshot, source, historical=True)
        missing = set(item.get("path") for item in audit.evidence.get("files", [])) - set(snapshot.files)
        AuditResult.objects.filter(pk=audit_id).update(original_error="固定提交缺少证据中的许可证文件。" if missing else "")
    return "stored"


def request_inspection(audit_id):
    with transaction.atomic():
        audit = AuditResult.objects.select_for_update().get(pk=audit_id)
        policy = LicensePolicy.objects.filter(active=True).first()
        sha = policy.source_sha if policy else ""
        check = audit.original_checks
        if check.get("policy") == sha and check.get("state") == "complete":
            return
        if check.get("state") == "running" and check.get("until", "") > timezone.now().isoformat():
            return
        audit.original_checks = {"state": "running", "policy": sha, "until": (timezone.now() + timedelta(hours=2)).isoformat()}
        audit.save(update_fields=["original_checks"])
        try:
            inspect_originals.delay(audit.pk)
        except Exception:
            audit.original_checks = {"state": "failed", "message": "任务服务暂不可用，请重试核验。"}
            audit.save(update_fields=["original_checks"])


@shared_task
def inspect_originals(audit_id):
    from licensecheck.engine import scan_candidates

    audit = AuditResult.objects.get(pk=audit_id)
    try:
        if not audit.originals.exists():
            retrieve_originals(audit_id)
        audit.refresh_from_db()
        policy = LicensePolicy.objects.filter(active=True).first()
        if not policy:
            result = {"state": "failed", "message": "尚无有效许可证策略，请先同步策略。"}
        elif not audit.originals.exists():
            result = {"state": "failed", "message": audit.original_error or "此固定提交没有可查看的许可证原文。"}
        else:
            files = {}
            for original in audit.originals.all():
                if not original.verified:
                    files[str(original.pk)] = {"decision": "invalid", "message": original.error}
                    continue
                scan = scan_candidates({"LICENSE": original.text}, policy.manifest, Path(policy.index_path))
                files[str(original.pk)] = {"decision": scan["decision"], "labels": scan["labels"]}
            result = {"state": "complete", "policy": policy.source_sha, "files": files}
    except Exception:
        result = {"state": "failed", "message": "原文核验或当前策略检查失败，请检查扫描服务后重试。"}
    AuditResult.objects.filter(pk=audit_id).update(original_checks=result)
    return result["state"]
