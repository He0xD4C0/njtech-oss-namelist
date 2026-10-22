"""Staff-only, CSRF-protected review endpoints; HTTP never scans or writes GitHub."""


from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from .approvals import request_approval, retry_approval
from .models import AuditResult, LicenseApproval, LicenseOriginal, LicensePolicy
from .originals import request_inspection
from .policy_github import PolicyWriteError


def staff(request):
    if not request.user.is_authenticated or not request.user.is_active or not request.user.is_staff:
        raise PermissionDenied


@require_GET
def originals(request, audit_id):
    staff(request)
    audit = get_object_or_404(AuditResult.objects.select_related("submission__repository"), pk=audit_id)
    policy = LicensePolicy.objects.filter(active=True).first()
    check = audit.original_checks
    rows = []
    for original in audit.originals.all():
        match = check.get("files", {}).get(str(original.pk), {}) if policy and check.get("policy") == policy.source_sha else {}
        # A fresh audit of the currently active policy is already authoritative.
        if not match and policy and audit.policy_sha == policy.source_sha:
            evidence = next((item for item in audit.evidence.get("files", []) if item.get("path") == original.path), {})
            keys = evidence.get("authorizing_policies", [])
            if evidence.get("complete_text_coverage") and keys and all(
                policy.manifest.get("policies", {}).get(key, {}).get("classification") == "whitelist" for key in keys
            ):
                match = {"decision": "published"}
        rows.append({"original": original, "allowed": match.get("decision") == "published",
                     "match": {"published": "当前策略完整允许", "rejected": "当前策略命中黑名单", "needs_review": "当前策略尚未完整允许", "invalid": "原文无效"}.get(match.get("decision"), "尚未核对当前策略"),
                     "job": LicenseApproval.objects.filter(digest=original.sha256).order_by("-pk").first()})
    auto_inspect = bool(policy and check.get("state") not in {"running", "failed"}
                        and (check.get("policy") != policy.source_sha or not rows))
    context = {**admin.site.each_context(request), "title": "许可证原文与审批", "audit": audit, "rows": rows, "auto_inspect": auto_inspect,
               "check": check, "removed": audit.submission.status == "removed", "opts": AuditResult._meta}
    return render(request, "admin/repositories/license_originals.html", context)


@require_POST
def inspect(request, audit_id):
    staff(request)
    get_object_or_404(AuditResult, pk=audit_id)
    request_inspection(audit_id)
    return redirect("admin:license_originals", audit_id)


@require_POST
def approve(request, original_id):
    staff(request)
    original = get_object_or_404(LicenseOriginal, pk=original_id)
    try:
        job = request_approval(original.pk, request.user)
    except (PolicyWriteError, ValueError) as error:
        messages.error(request, str(error))
        return redirect("admin:license_originals", original.audit_id)
    return redirect("admin:license_approval", job.pk)


@require_POST
def recheck(request, original_id):
    staff(request)
    original = get_object_or_404(LicenseOriginal.objects.select_related("audit__submission"), pk=original_id)
    from .tasks import schedule_audit
    if schedule_audit(original.audit.submission_id):
        messages.success(request, "已安排重新机审，仓库仍须通过公开性、归属及全部许可证文件检查。")
    else:
        messages.error(request, "申请已移除，不能重新机审。")
    return redirect("admin:license_originals", original.audit_id)


def job_data(job):
    labels = {"queued": "等待执行", "running": "执行中", "waiting": "等待后续任务", "blocked": "复审受阻", "failed": "执行失败", "completed": "已完成"}
    return {"stage": job.get_stage_display(), "status": labels.get(job.status, job.status),
            "reason": job.reason, "commit": job.commit_sha, "policy": job.policy_sha,
            "reaudit": list(job.reaudit.values()), "failed": job.status == "failed",
            "active": job.active, "updated": job.updated_at.isoformat()}


@require_GET
def approval(request, job_id):
    staff(request)
    job = get_object_or_404(LicenseApproval.objects.select_related("original__audit__submission__repository"), pk=job_id)
    context = {**admin.site.each_context(request), "title": "许可证审批进度", "job": job, "data": job_data(job),
               "stages": [label for _, label in LicenseApproval._meta.get_field("stage").choices if label != "已完成"],
               "opts": LicenseApproval._meta}
    return render(request, "admin/repositories/license_approval.html", context)


@require_GET
def status(request, job_id):
    staff(request)
    return JsonResponse(job_data(get_object_or_404(LicenseApproval, pk=job_id)))


@require_POST
def retry(request, job_id):
    staff(request)
    get_object_or_404(LicenseApproval, pk=job_id)
    try:
        retry_approval(job_id, request.user)
    except (PolicyWriteError, ValueError) as error:
        messages.error(request, str(error))
    return redirect("admin:license_approval", job_id)


@require_GET
def inspection_status(request, audit_id):
    staff(request)
    audit = get_object_or_404(AuditResult, pk=audit_id)
    return JsonResponse({"state": audit.original_checks.get("state", ""), "count": audit.originals.count()})
