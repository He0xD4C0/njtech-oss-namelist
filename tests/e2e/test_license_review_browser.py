import hashlib

import pytest
from allauth.account.models import EmailAddress
from django.urls import reverse
from playwright.sync_api import expect

from core.models import User
from core.runtime import set_setting
from repositories.models import (
    AuditDispatch,
    AuditResult,
    LicenseApproval,
    LicenseOriginal,
    LicensePolicy,
    Repository,
    Submission,
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.e2e, pytest.mark.usefixtures("initialized_site")]


@pytest.mark.parametrize("width", [1440, 390])
def test_staff_reads_full_plain_text_approves_and_recovers(page, live_server, session_login, artifact_dir, monkeypatch, width):
    user = User.objects.create_user("reviewer", password="Local-browser-review-42", is_staff=True)
    EmailAddress.objects.create(user=user, email="reviewer@example.test", verified=True, primary=True)
    repo = Repository.objects.create(platform="github", platform_id="42", owner_id="42", full_name="reviewer/project",
        clone_url="https://github.com/reviewer/project.git")
    submission = Submission.objects.create(user=user, repository=repo, status="needs_review", reason="旧许可证提示")
    audit = AuditResult.objects.create(submission=submission, generation=1, source_sha="a" * 40, policy_sha="b" * 40,
        detector_version="test", decision="needs_review")
    raw = ("Example Custom License\n\n" + "Every person may copy, change and redistribute this work without charge.\n" * 40
           + '<script>window.licenseExecuted = true</script>\nTHE COMPLETE FINAL LINE').encode()
    original = LicenseOriginal.objects.create(audit=audit, path="LICENSE", raw=raw, sha256=hashlib.sha256(raw).hexdigest(),
        source_url=repo.clone_url, source_sha=audit.source_sha, verified=True)
    audit.original_checks = {"state": "complete", "policy": "b" * 40, "files": {str(original.pk): {"decision": "needs_review"}}}
    audit.save(update_fields=["original_checks"])
    LicensePolicy.objects.create(source_sha="b" * 40, active=True, detector_version="test", manifest={}, index_path="unused")
    AuditDispatch.objects.create(submission=submission, generation=1, status="waiting_auth", reason="等待账号重新授权")
    set_setting("license_source_url", "https://github.com/owner/policy.git")
    set_setting("license_write_token", "github_pat_local_test_only")
    monkeypatch.setattr("repositories.approvals.enqueue", lambda pk: True)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    session_login(user)
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(live_server.url + "/admin/repositories/submission/?status__exact=needs_review")
    expect(page.get_by_text("等待重新授权：等待账号重新授权", exact=True)).to_be_visible()
    page.get_by_role("link", name="查看完整原文与审批", exact=True).click()
    expect(page.locator(".license-text")).to_have_text(raw.decode())
    assert page.locator(".license-text").evaluate("element => element.tagName") == "PRE"
    assert page.evaluate("window.licenseExecuted") is None
    assert "github_pat_" not in page.locator("body").inner_text()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(artifact_dir / f"license-original-{width}.png"), full_page=True)
    button = page.get_by_role("button", name="加入允许仓库并同步", exact=True)
    button.scroll_into_view_if_needed()
    expect(button).to_be_visible()
    button.click()
    expect(page.locator("[data-job-status]")).to_have_text("校验原文 · 等待执行")
    job = LicenseApproval.objects.get()
    assert job.original_id == original.pk and job.actor_id == user.pk
    LicenseApproval.objects.filter(pk=job.pk).update(stage="preflight", status="failed", reason="策略预检失败，尚未提交。")
    retry = page.get_by_role("button", name="从失败阶段重试", exact=True)
    expect(retry).to_be_visible(timeout=10000)
    page.screenshot(path=str(artifact_dir / f"license-failure-{width}.png"), full_page=True)
    retry.click()
    job.refresh_from_db()
    assert job.stage == "preflight" and job.status == "queued"
    LicenseApproval.objects.filter(pk=job.pk).update(stage="reaudit", status="blocked", commit_sha="c" * 40,
        policy_sha="c" * 40, reason="策略已生效，仓库复审等待重新授权。",
        reaudit={str(submission.pk): {"repository": repo.full_name, "status": "等待重新授权", "reason": "账号授权失效"}})
    expect(page.locator("[data-job-policy]")).to_have_text("c" * 40, timeout=10000)
    expect(page.locator("[data-job-reaudits]")).to_contain_text("等待重新授权")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(artifact_dir / f"license-progress-{width}.png"), full_page=True)
    submission.refresh_from_db()
    assert submission.status == "needs_review"
    User.objects.filter(pk=user.pk).update(is_staff=False)
    response = page.request.post(live_server.url + reverse("admin:license_approve", args=[original.pk]))
    assert response.status in {200, 403}  # A nonstaff admin request can redirect to its login page.
    assert LicenseApproval.objects.count() == 1
    assert not errors
