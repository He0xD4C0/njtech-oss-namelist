"""Durable delivery and deterministic-invalid-input regressions (remote transports mocked)."""

from unittest.mock import patch

import pytest
from django.utils import timezone

from core.crypto import encrypt
from core.git import GitLicenseInputError, GitSnapshot
from core.models import User
from core.runtime import set_setting
from core.tasks import sync_license_policy
from licensecheck.engine import CandidateValidationError, PolicyArtifactError
from providers.clients import RepoData
from providers.models import OAuthBinding
from repositories.models import AuditDispatch, AuditResult, LicensePolicy, Repository, Submission
from repositories.services import add_submission, remove_submission
from repositories.tasks import audit_submission, recover_audits

pytestmark = pytest.mark.django_db


@pytest.fixture
def context():
    user = User.objects.create_user(username="owner", email="owner@example.test")
    OAuthBinding.objects.create(
        user=user,
        platform="github",
        platform_user_id="123",
        username="owner",
        encrypted_access_token=encrypt("test-token"),
    )
    dto = RepoData(
        platform="github",
        platform_id="456",
        name="demo",
        full_name="owner/demo",
        description="Public",
        html_url="https://github.com/owner/demo",
        clone_url="https://github.com/owner/demo.git",
        owner_id="123",
        default_branch="main",
        fork=False,
        archived=False,
        stars=10,
        public=True,
        owner_type="user",
        head_sha="c" * 40,
        commit_at=timezone.now(),
    )
    return user, dto


def make_published(context, version="v1"):
    user, dto = context
    repo = Repository.objects.create(**dto.as_metadata())
    policy = LicensePolicy.objects.create(
        source_sha="b" * 40, detector_version=version, manifest={}, index_path="unused", active=True
    )
    claim = Submission.objects.create(
        user=user,
        repository=repo,
        status="published",
        published_at=timezone.now(),
        baseline_at=timezone.now(),
        baseline_stars=10,
        audited_head="a" * 40,
        audited_policy=policy.source_sha,
        audited_detector=version,
        public_metadata={"description": "Last approved description"},
    )
    return claim, policy


def test_broker_disconnect_after_commit_cannot_lose_new_submission(
    context, django_capture_on_commit_callbacks
):
    user, dto = context
    with (
        patch("providers.api.validate_repository", return_value=dto),
        patch("repositories.tasks.audit_submission.delay", side_effect=ConnectionError("broker down")),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            claim = add_submission(user, "github", "456")
    job = AuditDispatch.objects.get(submission=claim)
    assert job.dispatched_at is None and job.completed_at is None
    with patch("repositories.tasks.audit_submission.delay") as enqueue:
        assert recover_audits() == 1
        enqueue.assert_called_once_with(claim.pk, 1)
        assert recover_audits() == 0
    remove_submission(claim, user)
    job.refresh_from_db()
    AuditDispatch.objects.filter(pk=job.pk).update(dispatched_at=None)
    with patch("repositories.tasks.audit_submission.delay") as enqueue:
        assert recover_audits() == 0
        enqueue.assert_not_called()


def test_policy_activation_reaudits_survive_broker_loss_and_unchanged_sync(
    context, tmp_path, settings, django_capture_on_commit_callbacks
):
    settings.WORK_ROOT = tmp_path
    claim, old = make_published(context)
    set_setting("license_source_url", "https://github.com/acme/rules.git")
    source = GitSnapshot("d" * 40, timezone.now(), {"whitelist/file": "policy"})
    with (
        patch("core.git.fetch_snapshot", return_value=source),
        patch("licensecheck.engine.compile_policy", return_value={"detector_version": "v2"}),
        patch("repositories.tasks.audit_submission.delay", side_effect=ConnectionError("broker down")),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            assert sync_license_policy() == source.sha
    job = AuditDispatch.objects.get(submission=claim)
    assert job.dispatched_at is None
    assert LicensePolicy.objects.get(active=True).source_sha == source.sha
    with (
        patch("core.git.fetch_snapshot", return_value=source),
        patch("licensecheck.engine.detector_version", return_value="v2"),
        patch("licensecheck.engine.validate_policy_artifacts"),
    ):
        assert sync_license_policy() == "unchanged"
    with patch("repositories.tasks.audit_submission.delay") as enqueue:
        assert recover_audits() == 1
        enqueue.assert_called_once_with(claim.pk, 2)


def test_detector_change_same_two_shas_is_not_skipped(context):
    claim, policy = make_published(context)
    Submission.objects.filter(pk=claim.pk).update(audited_head="c" * 40, audited_detector="old-version")
    result = {
        "decision": "published",
        "labels": ["MIT"],
        "evidence": {"files": []},
        "detector_version": policy.detector_version,
    }
    with (
        patch("providers.api.validate_repository", return_value=context[1]),
        patch(
            "core.git.fetch_snapshot", return_value=GitSnapshot("c" * 40, timezone.now(), {"LICENSE": "text"})
        ),
        patch("licensecheck.engine.scan_candidates", return_value=result) as scan,
    ):
        assert audit_submission(claim.pk, 1) == "published"
        scan.assert_called_once()
    claim.refresh_from_db()
    assert claim.audited_detector == "v1" and AuditResult.objects.count() == 1


@pytest.mark.parametrize("kind", ["nul", "oversized"])
def test_deterministic_bad_source_cannot_keep_published_conclusion(context, kind):
    claim, policy = make_published(context)
    source = GitSnapshot("c" * 40, timezone.now(), {"LICENSE": "Noncommercial only.\0"})
    fetch = (
        patch("core.git.fetch_snapshot", return_value=source)
        if kind == "nul"
        else patch(
            "core.git.fetch_snapshot",
            side_effect=GitLicenseInputError(
                "c" * 40,
                timezone.now(),
                "license_text_exceeds_scan_limits",
                [{"path": "LICENSE", "size": 2_000_000}],
            ),
        )
    )
    with (
        patch("providers.api.validate_repository", return_value=context[1]),
        fetch,
        patch("licensecheck.engine.scan_candidates", side_effect=CandidateValidationError("invalid text")),
    ):
        assert audit_submission(claim.pk, 1) == "historical"
    claim.refresh_from_db()
    assert claim.public_metadata["description"] == "Last approved description"
    assert claim.audited_head == "c" * 40
    assert AuditResult.objects.get(submission=claim).decision == "needs_review"


def test_runtime_artifact_failure_preserves_last_verified_state(context):
    claim, policy = make_published(context)
    with (
        patch("providers.api.validate_repository", return_value=context[1]),
        patch(
            "core.git.fetch_snapshot", return_value=GitSnapshot("c" * 40, timezone.now(), {"LICENSE": "text"})
        ),
        patch(
            "licensecheck.engine.scan_candidates", side_effect=PolicyArtifactError("runtime index corrupt")
        ),
    ):
        with pytest.raises(PolicyArtifactError):
            audit_submission(claim.pk, 1)
    claim.refresh_from_db()
    assert claim.status == "published" and claim.audited_head == "a" * 40


def test_no_license_is_definitive_even_if_detector_cache_is_unavailable(context):
    claim, policy = make_published(context)
    with (
        patch("providers.api.validate_repository", return_value=context[1]),
        patch("core.git.fetch_snapshot", return_value=GitSnapshot("c" * 40, timezone.now(), {})),
        patch(
            "licensecheck.engine.scan_candidates", side_effect=PolicyArtifactError("cache missing")
        ) as scan,
    ):
        assert audit_submission(claim.pk, 1) == "historical"
        scan.assert_not_called()
    assert AuditResult.objects.get(submission=claim).evidence["reason"] == "no_license_candidates"
