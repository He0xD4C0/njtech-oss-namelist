import pytest
from django.utils import timezone

from core.crypto import encrypt
from core.models import User
from core.services import delete_account, disconnect_platform
from providers.models import OAuthBinding
from repositories.models import AuditResult, LicensePolicy, Repository, Submission
from repositories.services import apply_audit_result, mark_ineligible, remove_submission

pytestmark = pytest.mark.django_db


@pytest.fixture
def claim():
    user = User.objects.create_user(
        username="owner", email="owner@example.org", password="test-owner-password!444"
    )
    OAuthBinding.objects.create(
        user=user,
        platform="github",
        platform_user_id="123",
        username="owner",
        encrypted_access_token=encrypt("token"),
    )
    repo = Repository.objects.create(
        platform="github",
        platform_id="456",
        name="repo",
        full_name="owner/repo",
        owner_id="123",
        html_url="https://github.com/owner/repo",
        clone_url="https://github.com/owner/repo.git",
        head_sha="a" * 40,
        stars=10,
    )
    return Submission.objects.create(user=user, repository=repo)


def policy():
    return LicensePolicy.objects.create(
        source_sha="b" * 40,
        detector_version="scancode/test",
        manifest={},
        index_path="/not-used",
        active=True,
    )


def passed():
    return {
        "decision": "published",
        "labels": ["MIT"],
        "evidence": {"matches": []},
        "detector_version": "scancode/test",
    }


def metadata(claim):
    repo = claim.repository
    return {
        "name": repo.name,
        "full_name": repo.full_name,
        "owner_id": repo.owner_id,
        "html_url": repo.html_url,
        "clone_url": repo.clone_url,
        "default_branch": "main",
        "stars": 10,
        "fork": False,
        "archived": True,
        "description": "public description",
    }


def test_archived_eligible_and_recheck_preserves_baseline(claim):
    pol = policy()
    assert (
        apply_audit_result(claim.pk, 1, "a" * 40, pol, passed(), metadata(claim), timezone.now())
        == "published"
    )
    claim.refresh_from_db()
    baseline = claim.baseline_at
    assert claim.public_metadata["archived"] is True
    assert (
        apply_audit_result(claim.pk, 1, "a" * 40, pol, passed(), metadata(claim), timezone.now())
        == "published"
    )
    claim.refresh_from_db()
    assert claim.baseline_at == baseline


def test_stale_job_cannot_republish_after_remove_or_unlink(claim):
    pol = policy()
    remove_submission(claim, claim.user)
    assert (
        apply_audit_result(claim.pk, 1, "a" * 40, pol, passed(), metadata(claim), timezone.now())
        == "superseded"
    )
    claim.refresh_from_db()
    assert claim.status == "removed" and not AuditResult.objects.exists()
    disconnect_platform(claim.user, "github")
    assert not OAuthBinding.objects.exists()


def test_old_policy_or_new_head_cannot_publish(claim):
    pol = policy()
    LicensePolicy.objects.filter(pk=pol.pk).update(active=False)
    assert (
        apply_audit_result(claim.pk, 1, "a" * 40, pol, passed(), metadata(claim), timezone.now())
        == "superseded"
    )
    LicensePolicy.objects.filter(pk=pol.pk).update(active=True)
    Repository.objects.filter(pk=claim.repository_id).update(head_sha="c" * 40)
    assert (
        apply_audit_result(claim.pk, 1, "a" * 40, pol, passed(), metadata(claim), timezone.now())
        == "superseded-head"
    )


def test_recompiled_same_sha_policy_cannot_accept_old_detector_result(claim):
    pol = policy()
    LicensePolicy.objects.filter(pk=pol.pk).update(detector_version="new-detector", manifest={"version": 2})
    assert (
        apply_audit_result(claim.pk, 1, "a" * 40, pol, passed(), metadata(claim), timezone.now())
        == "superseded"
    )


def test_license_loss_becomes_history_last_public_metadata_survives(claim):
    pol = policy()
    apply_audit_result(claim.pk, 1, "a" * 40, pol, passed(), metadata(claim), timezone.now())
    failure = {**passed(), "decision": "rejected", "labels": []}
    Submission.objects.filter(pk=claim.pk).update(generation=2)
    Repository.objects.filter(pk=claim.repository_id).update(head_sha="c" * 40)
    assert (
        apply_audit_result(
            claim.pk,
            2,
            "c" * 40,
            pol,
            failure,
            {**metadata(claim), "description": "not-public"},
            timezone.now(),
        )
        == "historical"
    )
    claim.refresh_from_db()
    assert claim.public_metadata["description"] == "public description"
    assert mark_ineligible(claim.pk, 2, "ownership lost")
    claim.refresh_from_db()
    assert claim.status == "historical"


def test_account_delete_withdraws_claims_and_credentials(claim):
    delete_account(claim.user)
    assert not Submission.objects.exists() and not OAuthBinding.objects.exists()
    assert Repository.objects.exists()
