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
