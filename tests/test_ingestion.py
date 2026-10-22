"""Full local ingestion chain with REAL ScanCode; only remote APIs/Git are fixtures.

This does not replace the required live OAuth/provider acceptance before release.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from core.crypto import encrypt
from core.git import GitSnapshot
from core.models import User
from licensecheck import spdx
from licensecheck.engine import compile_policy
from providers.clients import RepoData
from providers.models import OAuthBinding
from repositories.models import AuditResult, LicensePolicy
from repositories.services import add_submission, remove_submission
from repositories.tasks import audit_submission

pytestmark = [pytest.mark.scancode, pytest.mark.django_db]
pytest.importorskip("licensedcode", reason="Real scanner group required")

TEXT = """Example Open Permission

Copyright (c) 2023 Open Example

Every person receiving this work may use, copy, change, publish, redistribute
and sell it for any purpose without payment or other condition. No warranty is
given and no creator accepts liability for any damage arising from this work.
"""


def test_approval_real_preflight_sync_and_ownership_gated_reaudit(tmp_path, settings, monkeypatch):
    import httpx
    from test_license_approval import FakeGitHub

    from core.models import SourceSyncState
    from core.runtime import set_setting
    from core.tasks import sync_license_policy
    from repositories.approvals import process_approval, request_approval
    from repositories.models import LicenseApproval, Repository, Submission
    from repositories.originals import save_originals
    from repositories.policy_github import PolicyGitHub

    settings.WORK_ROOT = tmp_path
    remote = FakeGitHub()
    remote.files = {"whitelist/mit.txt": spdx.templates()["MIT"]["text"], "sources.json": '{"files":[]}'}
    remote.head = remote.make_head(remote.files)
    target = "https://github.com/owner/policy.git"
    set_setting("license_source_url", target)
    set_setting("license_write_token", "github_pat_local_protocol_fixture")
    client = PolicyGitHub(target, lambda: None, transport=httpx.MockTransport(remote))
    monkeypatch.setattr("repositories.approvals.PolicyGitHub", lambda destination, guard: PolicyGitHub(destination, guard, transport=httpx.MockTransport(remote)))
    monkeypatch.setattr("core.tasks.sync_license_policy.delay", lambda **kwargs: None)
    monkeypatch.setattr("repositories.tasks.audit_submission.delay", lambda *args: None)
    old_index = tmp_path / "old-policy"
    manifest = compile_policy({"whitelist/mit.txt": remote.files["whitelist/mit.txt"]}, old_index)
    policy = LicensePolicy.objects.create(source_sha=remote.head, active=True, detector_version=manifest["detector_version"], manifest=manifest, index_path=str(old_index))
    owner = User.objects.create_user("owner", is_staff=True)
    OAuthBinding.objects.create(user=owner, platform="github", platform_user_id="123", username="owner", encrypted_access_token=encrypt("local-identity-fixture"))
    data = RepoData(platform="github", platform_id="456", name="demo", full_name="owner/demo", description="Local fixture",
        html_url="https://github.com/owner/demo", clone_url="https://github.com/owner/demo.git", owner_id="123", default_branch="main",
        fork=False, archived=False, stars=10, public=True, owner_type="user", head_sha="a" * 40, commit_at=timezone.now())
    repository = Repository.objects.create(**data.as_metadata())
    submission = Submission.objects.create(user=owner, repository=repository, status="needs_review")
    audit = AuditResult.objects.create(submission=submission, generation=1, source_sha="a" * 40, policy_sha=policy.source_sha,
        detector_version=policy.detector_version, decision="needs_review")
    snapshot = GitSnapshot(audit.source_sha, timezone.now(), {"LICENSE": TEXT})
    save_originals(audit, snapshot, data.clone_url)
    job = request_approval(audit.originals.get().pk, owner)
    assert process_approval(job.pk) == "waiting"
    job.refresh_from_db()
    assert remote.advances == 1 and job.commit_sha and job.stage == "sync"
    policy_snapshot = GitSnapshot(remote.head, timezone.now(), client.read().files)
    with patch("core.git.fetch_snapshot", return_value=policy_snapshot):
        sync_license_policy(run_id=job.sync_run_id, source=target, generation=SourceSyncState.objects.get(kind="licenses").generation)
    submission.refresh_from_db()
    assert submission.status == "needs_review" and submission.generation == 2
    # Admission still follows the normal public/ownership checks and real scanner.
    with patch("providers.api.validate_repository", return_value=data) as ownership, patch("core.git.fetch_snapshot", return_value=snapshot):
        assert audit_submission(submission.pk, submission.generation) == "published"
        assert ownership.call_count == 2
    LicenseApproval.objects.filter(pk=job.pk).update(retry_at=None)
    process_approval(job.pk)
    job.refresh_from_db()
    assert job.stage == "done" and not job.active and job.policy_sha == remote.head
    assert next(iter(job.reaudit.values()))["status"] == "公开"


@pytest.mark.parametrize(
    "platform,host", [("github", "github.com"), ("gitee", "gitee.com"), ("gitlab", "gitlab.com")]
)
@pytest.mark.parametrize("standard", [False, True], ids=["custom-policy", "spdx-variables"])
def test_submit_real_scan_publish_and_stale_job_withdraw(
    platform, host, standard, tmp_path, django_capture_on_commit_callbacks, client, initialized_site
):
    index = tmp_path / "index"
    policy_text = spdx.templates()["MIT"]["text"] if standard else TEXT
    manifest = compile_policy({"whitelist/arbitrary-name": policy_text}, index)
    LicensePolicy.objects.create(
        source_sha="b" * 40,
        active=True,
        detector_version=manifest["detector_version"],
        manifest=manifest,
        index_path=str(index),
    )
    owner = User.objects.create_user(
        username="owner", email="owner@example.test", password="test-safe-owner-444"
    )
    OAuthBinding.objects.create(
        user=owner,
        platform=platform,
        platform_user_id="123",
        username="owner",
        encrypted_access_token=encrypt("test-token"),
    )
    at = timezone.now() - timedelta(days=1)
    data = RepoData(
        platform=platform,
        platform_id="456",
        name="demo",
        full_name="owner/demo",
        description="Public test repository",
        html_url=f"https://{host}/owner/demo",
        clone_url=f"https://{host}/owner/demo.git",
        owner_id="123",
        default_branch="main",
        fork=True,
        archived=True,
        stars=10,
        public=True,
        owner_type="user",
        head_sha="a" * 40,
        commit_at=at,
    )
    # ScanCode parses Bob Smith as a holder; "Another Author" is JUNK/AUTH and
    # remains uncertain, covered separately by the license-engine negative test.
    candidate = (policy_text.replace("MIT License", "").replace("<year> <copyright holders>", "2026 张三")
                 if standard else TEXT.replace("2023 Open Example", "2026 Bob Smith"))
    source = GitSnapshot("a" * 40, at, {"LICENSE": candidate})
    with (
        patch("providers.api.validate_repository", return_value=data),
        patch("repositories.tasks.audit_submission.delay") as enqueue,
    ):
        with django_capture_on_commit_callbacks(execute=True):
            claim = add_submission(owner, platform, "456")
        enqueue.assert_called_once_with(claim.pk, 1)
    with (
        patch("providers.api.validate_repository", return_value=data),
        patch("core.git.fetch_snapshot", return_value=source),
    ):
        assert audit_submission(claim.pk, 1) == "published"
    claim.refresh_from_db()
    assert claim.status == "published" and claim.public_metadata["archived"] is True
    evidence = AuditResult.objects.get(submission=claim)
    assert evidence.source_sha == source.sha and evidence.policy_sha == "b" * 40
    assert evidence.detector_version.startswith("scancode-toolkit/")
    response = client.get("/repositories/")
    assert response.status_code == 200
    assert "owner/demo" in response.content.decode()
    remove_submission(claim, owner)
    assert audit_submission(claim.pk, 1) == "superseded"
    claim.refresh_from_db()
    assert claim.status == "removed"
