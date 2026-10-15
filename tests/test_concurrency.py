"""Database locking proofs executed by the PostgreSQL test job, not simulated in SQLite."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connection, connections
from django.utils import timezone

from core.crypto import encrypt
from core.models import User
from providers.clients import RepoData
from providers.models import OAuthBinding
from repositories.models import LicensePolicy, Repository, Submission
from repositories.services import add_submission, apply_audit_result

pytestmark = pytest.mark.django_db(transaction=True)


def test_concurrent_additions_cannot_exceed_quota():
    if connection.vendor != "postgresql":
        pytest.skip("Row-lock acceptance requires PostgreSQL")
    owner = User.objects.create_user(username="owner", email="owner@example.test", quota=1)
    OAuthBinding.objects.create(
        user=owner,
        platform="github",
        platform_user_id="123",
        username="owner",
        encrypted_access_token=encrypt("test-token"),
    )
    barrier = Barrier(2)

    def fresh(binding, rid):
        return RepoData(
            platform="github",
            platform_id=rid,
            name="demo",
            full_name=f"owner/demo-{rid}",
            description="Public test",
            html_url=f"https://github.com/owner/demo-{rid}",
            clone_url=f"https://github.com/owner/demo-{rid}.git",
            owner_id="123",
            default_branch="main",
            fork=False,
            archived=False,
            stars=0,
            public=True,
            owner_type="user",
        )

    def submit(rid):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            add_submission(owner, "github", rid)
            return "created"
        except ValueError:
            return "quota"
        finally:
            connections.close_all()

    with (
        patch("providers.api.validate_repository", side_effect=fresh),
        patch("repositories.tasks.audit_submission.delay"),
    ):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, ("456", "457")))
    assert sorted(results) == ["created", "quota"]
    assert Submission.objects.filter(user=owner).exclude(status="removed").count() == 1


def test_transfer_and_old_audit_share_lock_order_and_cannot_republish_old_claim():
    if connection.vendor != "postgresql":
        pytest.skip("Row-lock acceptance requires PostgreSQL")
    old = User.objects.create_user(username="former", email="former@example.test")
    new = User.objects.create_user(username="current", email="current@example.test")
    for user, uid in ((old, "1"), (new, "2")):
        OAuthBinding.objects.create(
            user=user,
            platform="github",
            platform_user_id=uid,
            username=user.username,
            encrypted_access_token=encrypt("test"),
        )
    repo = Repository.objects.create(
        platform="github",
        platform_id="999",
        name="demo",
        full_name="former/demo",
        owner_id="1",
        html_url="https://github.com/former/demo",
        clone_url="https://github.com/former/demo.git",
        head_sha="a" * 40,
    )
    claim = Submission.objects.create(
        user=old,
        repository=repo,
        status="published",
        published_at=timezone.now(),
        baseline_at=timezone.now(),
        baseline_stars=1,
        public_metadata={"full_name": "former/demo"},
    )
    policy = LicensePolicy.objects.create(
        source_sha="b" * 40, detector_version="test", index_path="unused", manifest={}, active=True
    )
    fresh = RepoData(
        platform="github",
        platform_id="999",
        name="demo",
        full_name="current/demo",
        description="Public",
        html_url="https://github.com/current/demo",
        clone_url="https://github.com/current/demo.git",
        owner_id="2",
        default_branch="main",
        fork=False,
        archived=False,
        stars=1,
        public=True,
        owner_type="user",
    )
    barrier = Barrier(2)

    def operation(kind):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            if kind == "transfer":
                return add_submission(new, "github", "999").status
            return apply_audit_result(
                claim.pk,
                1,
                "a" * 40,
                policy,
                {"decision": "published", "labels": ["MIT"], "evidence": {}, "detector_version": "test"},
                {"stars": 1, "owner_id": "1", "full_name": "former/demo"},
                timezone.now(),
            )
        finally:
            connections.close_all()

    with (
        patch("providers.api.validate_repository", return_value=fresh),
        patch("repositories.tasks.audit_submission.delay"),
    ):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(operation, ("transfer", "audit")))
    claim.refresh_from_db()
    assert claim.status == "historical" and claim.generation == 2
    assert Submission.objects.get(user=new, repository=repo).status == "queued"
