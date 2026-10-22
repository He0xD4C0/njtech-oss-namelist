"""Local Git Database protocol simulation; never contacts or writes a real repository."""

import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import httpx
import pytest
from allauth.account.models import EmailAddress
from django.core.exceptions import ValidationError
from django.db import connection, connections
from django.test import Client
from django.urls import reverse

from core.git import GitSnapshot
from core.models import RuntimeSetting, SyncRun, User
from core.runtime import set_setting
from repositories.approvals import process_approval, request_approval, retry_approval
from repositories.models import (
    AuditDispatch,
    AuditResult,
    LicenseApproval,
    LicenseOriginal,
    LicensePolicy,
    Repository,
    Submission,
)
from repositories.originals import retrieve_originals, save_originals
from repositories.policy_github import PolicyGitHub

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("initialized_site")]
TEXT = 'Everyone may use and redistribute this work without restriction.\n<script>window.licenseExecuted = true</script>\n'


@pytest.fixture
def original():
    staff = User.objects.create_user("reviewer", is_staff=True, password="Local-review-test-42")
    EmailAddress.objects.create(user=staff, email="reviewer@example.test", primary=True, verified=True)
    repo = Repository.objects.create(platform="github", platform_id="42", full_name="reviewer/project", owner_id="42",
                                     clone_url="https://github.com/reviewer/project.git")
    submission = Submission.objects.create(user=staff, repository=repo, status="needs_review")
    audit = AuditResult.objects.create(submission=submission, generation=1, source_sha="a" * 40, policy_sha="b" * 40,
        detector_version="test", decision="needs_review", evidence={"files": [{"path": "LICENSE", "sha256": hashlib.sha256(TEXT.encode()).hexdigest()}]})
    save_originals(audit, GitSnapshot(audit.source_sha, None, {"LICENSE": TEXT}), repo.clone_url)
    set_setting("license_source_url", "https://github.com/owner/policy.git")
    set_setting("license_write_token", "github_pat_local_fixture_only")
    return audit.originals.get()


class FakeGitHub:
    def __init__(self, *, move=False, lose_response=False):
        self.blobs, self.trees, self.commits, self.requests = {}, {}, {}, []
        self.files = {"whitelist/existing.txt": "Existing license.", "sources.json": '{"files":[]}'}
        self.head = self.make_head(self.files)
        self.move, self.lose_response = move, lose_response
        self.advances = 0

    def store(self, mapping, value):
        key = hashlib.sha1(json.dumps(value, sort_keys=True).encode()).hexdigest()
        mapping[key] = value
        return key

    def make_head(self, files):
        tree = []
        for path, text in files.items():
            raw = text.encode()
            blob = self.store(self.blobs, {"encoding": "base64", "content": base64.b64encode(raw).decode()})
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob, "size": len(raw)})
        tree_sha = self.store(self.trees, tree)
        return self.store(self.commits, {"tree": {"sha": tree_sha}})

    def __call__(self, request):
        assert request.url.host == "api.github.com"
        assert request.url.path.startswith("/repos/owner/policy")
        self.requests.append(request)
        path = request.url.path.removeprefix("/repos/owner/policy")
        data = json.loads(request.content) if request.content else None
        if request.method == "GET":
            if not path:
                value = {"full_name": "owner/policy", "private": False, "default_branch": "main"}
            elif path == "/git/ref/heads/main":
                value = {"object": {"sha": self.head}}
            elif path.startswith("/git/commits/"):
                value = self.commits[path.rsplit("/", 1)[1]]
            elif path.startswith("/git/trees/"):
                value = {"tree": self.trees[path.rsplit("/", 1)[1]], "truncated": False}
            else:
                value = self.blobs[path.rsplit("/", 1)[1]]
            return httpx.Response(200, json=value)
        if request.method == "PATCH":
            assert data["force"] is False
            commit = self.commits[data["sha"]]
            if self.move:
                self.move -= 1
                self.files["whitelist/concurrent.txt"] = f"Another administrator's text. {self.move}"
                self.head = self.make_head(self.files)
                return httpx.Response(422, json={})
            assert commit["parents"] == [self.head]
            self.head = data["sha"]
            self.advances += 1
            if self.lose_response:
                self.lose_response = False
                raise httpx.ReadTimeout("private response lost")
            return httpx.Response(200, json={"object": {"sha": self.head}})
        if path == "/git/blobs":
            sha = self.store(self.blobs, data)
        elif path == "/git/trees":
            tree = {entry["path"]: entry for entry in self.trees[data["base_tree"]]}
            tree.update({entry["path"]: entry for entry in data["tree"]})
            sha = self.store(self.trees, list(tree.values()))
        elif path == "/git/commits":
            assert "Co-Authored-By" not in data["message"]
            sha = self.store(self.commits, {**data, "tree": {"sha": data["tree"]}})
        else:
            pytest.fail(f"Unexpected protocol operation: {path}")
        return httpx.Response(201, json={"sha": sha})


@pytest.fixture
def protocol(monkeypatch, settings, tmp_path):
    settings.WORK_ROOT = tmp_path
    def install(**options):
        fake = FakeGitHub(**options)
        monkeypatch.setattr("repositories.approvals.PolicyGitHub", lambda target, guard: PolicyGitHub(target, guard, transport=httpx.MockTransport(fake)))
        monkeypatch.setattr("licensecheck.review.preflight", lambda *args: "add")
        run = SyncRun.objects.create(kind="licenses", status="queued")
        monkeypatch.setattr("repositories.approvals.queue_sync", lambda kind: (run, True))
        return fake, run
    return install


def test_duplicate_clicks_reuse_one_task_and_original_cannot_change(original):
    user = original.audit.submission.user
    one = request_approval(original.pk, user)
    assert request_approval(original.pk, user).pk == one.pk
    assert LicenseApproval.objects.count() == 1
    original.raw = b"changed"
    with pytest.raises(ValidationError):
        original.save()


def test_account_deletion_is_not_blocked_by_approval_history(original):
    user = original.audit.submission.user
    job = request_approval(original.pk, user)
    user.delete()
    job.refresh_from_db()
    assert job.original_id is None and job.actor_id is None and job.digest == original.sha256
    assert process_approval(job.pk) == "failed"


@pytest.mark.parametrize("options", [{}, {"move": True}, {"lose_response": True}])
def test_atomic_git_commit_rebase_and_uncertain_write_recovery(original, protocol, options):
    remote, run = protocol(**options)
    job = request_approval(original.pk, original.audit.submission.user)
    assert process_approval(job.pk) == "waiting"
    job.refresh_from_db()
    if options.get("lose_response"):
        assert not job.commit_sha and job.proposed_sha
        LicenseApproval.objects.filter(pk=job.pk).update(retry_at=None)
        assert process_approval(job.pk) == "waiting"
        job.refresh_from_db()
    assert remote.advances == 1 and job.stage == "sync" and job.commit_sha
    tree = remote.trees[remote.commits[remote.head]["tree"]["sha"]]
    paths = {entry["path"] for entry in tree}
    assert f"whitelist/reviewed-{original.sha256}.txt" in paths and "sources.json" in paths
    if options.get("move"):
        assert "whitelist/concurrent.txt" in paths
    sources = json.loads(base64.b64decode(remote.blobs[next(entry["sha"] for entry in tree if entry["path"] == "sources.json")]["content"]))
    entry = sources["files"][0]
    assert entry["sha256"] == original.sha256 and entry["source"]["commit"] == original.source_sha
    assert "spdx_id" not in entry and "osi_approved" not in entry
    # A failed sync resumes after the already-confirmed Git commit.
    SyncRun.objects.filter(pk=run.pk).update(status="failed")
    LicenseApproval.objects.filter(pk=job.pk).update(retry_at=None)
    assert process_approval(job.pk) == "failed"
    retry_approval(job.pk, original.audit.submission.user)
    SyncRun.objects.filter(pk=run.pk).update(status="queued")
    assert process_approval(job.pk) == "waiting"
    assert remote.advances == 1


def test_policy_activation_reports_authorization_block_instead_of_publishing(original, protocol, monkeypatch):
    remote, _ = protocol()
    job = request_approval(original.pk, original.audit.submission.user)
    process_approval(job.pk)
    job.refresh_from_db()
    manifest = {"policies": {"reviewed": {"classification": "whitelist", "sources": [{
        "path": f"whitelist/reviewed-{original.sha256}.txt", "sha256": original.sha256}]}}}
    LicensePolicy.objects.create(source_sha=job.commit_sha, active=True, detector_version="test", manifest=manifest, index_path="unused")
    monkeypatch.setattr("licensecheck.engine.scan_candidates", lambda *args: {"decision": "published"})
    AuditDispatch.objects.create(submission=original.audit.submission, generation=2, status="waiting_auth", reason="等待重新授权")
    Submission.objects.filter(pk=original.audit.submission_id).update(generation=2)
    LicenseApproval.objects.filter(pk=job.pk).update(retry_at=None)
    process_approval(job.pk)
    job.refresh_from_db()
    assert job.policy_sha == job.commit_sha and job.status == "blocked"
    assert next(iter(job.reaudit.values()))["status"] == "等待重新授权"
    original.audit.submission.refresh_from_db()
    assert original.audit.submission.status == "needs_review" and remote.advances == 1


def test_preflight_conflict_makes_no_git_writes(original, protocol, monkeypatch):
    from licensecheck.engine import PolicyCompilationError
    remote, _ = protocol()
    def conflict(*args):
        raise PolicyCompilationError("secret diagnostics")
    monkeypatch.setattr("licensecheck.review.preflight", conflict)
    job = request_approval(original.pk, original.audit.submission.user)
    assert process_approval(job.pk) == "failed"
    assert all(request.method == "GET" for request in remote.requests)
    job.refresh_from_db()
    assert "secret" not in job.reason


def test_three_branch_conflicts_stop_without_force_or_overwrite(original, protocol):
    remote, _ = protocol(move=3)
    job = request_approval(original.pk, original.audit.submission.user)
    assert process_approval(job.pk) == "failed"
    assert remote.advances == 0
    assert sum(request.method == "PATCH" for request in remote.requests) == 3
    job.refresh_from_db()
    assert "三次" in job.reason and not job.commit_sha


@pytest.mark.parametrize("change", ["removed", "permission", "target"])
def test_pending_job_rechecks_permission_and_target_before_network(original, protocol, change):
    remote, _ = protocol()
    job = request_approval(original.pk, original.audit.submission.user)
    if change == "removed":
        Submission.objects.filter(pk=original.audit.submission_id).update(status="removed")
    elif change == "permission":
        User.objects.filter(pk=job.actor_id).update(is_staff=False)
    else:
        set_setting("license_source_url", "https://github.com/different/policy.git")
    assert process_approval(job.pk) == "failed"
    assert not remote.requests


def test_historical_original_is_pinned_and_hash_verified(original):
    audit = original.audit
    LicenseOriginal.objects.filter(pk=original.pk).delete()
    with patch("repositories.originals.fetch_snapshot", return_value=GitSnapshot(audit.source_sha, None, {"LICENSE": "new branch text"})) as fetch:
        assert retrieve_originals(audit.pk) == "stored"
    fetch.assert_called_once_with(audit.submission.repository.clone_url, revision=audit.source_sha)
    stored = audit.originals.get()
    assert not stored.verified and "哈希" in stored.error
    assert audit.evidence["files"][0]["sha256"] == original.sha256


def test_staff_can_review_and_post_but_credentials_stay_private(original, client):
    staff = original.audit.submission.user
    client.force_login(staff)
    url = reverse("admin:license_originals", args=[original.audit_id])
    response = client.get(url)
    assert response.status_code == 200
    content = response.content.decode()
    assert "&lt;script&gt;" in content and "<script>window.licenseExecuted" not in content
    assert TEXT.splitlines()[0] in content and "加入允许仓库并同步" in content
    assert "github_pat_" not in content
    encrypted = RuntimeSetting.objects.get(key="license_write_token")
    assert encrypted.secret and "github_pat_" not in encrypted.value
    assert client.get("/manage/settings/", follow=True).status_code == 403
    response = client.post("/manage/settings/", {
        "action": "save", "group": "licenses", "license_write_token": "github_pat_unpermitted",
    })
    assert response.status_code == 403
    assert RuntimeSetting.objects.get(pk=encrypted.pk).value == encrypted.value
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(staff)
    write_url = reverse("admin:license_approve", args=[original.pk])
    assert strict.get(write_url).status_code == 405
    assert strict.post(write_url).status_code == 403
    response = client.post(write_url)
    assert response.status_code == 302 and LicenseApproval.objects.count() == 1
    User.objects.filter(pk=staff.pk).update(is_staff=False)
    assert client.post(write_url).status_code in {302, 403}
    assert LicenseApproval.objects.count() == 1


@pytest.mark.parametrize("invalid", ["removed", "unverified", "empty", "encoding", "hash"])
def test_invalid_original_post_cannot_create_approval(original, client, invalid):
    client.force_login(original.audit.submission.user)
    if invalid == "removed":
        Submission.objects.filter(pk=original.audit.submission_id).update(status="removed")
    else:
        values = {
            "unverified": {"verified": False}, "empty": {"raw": b""},
            "encoding": {"raw": b"\xff", "sha256": hashlib.sha256(b"\xff").hexdigest()},
            "hash": {"sha256": "0" * 64},
        }[invalid]
        LicenseOriginal.objects.filter(pk=original.pk).update(**values)
    response = client.post(reverse("admin:license_approve", args=[original.pk]), follow=True)
    assert response.status_code == 200 and not LicenseApproval.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("different_original", [False, True])
def test_concurrent_identical_approvals_share_task(original, monkeypatch, different_original):
    if connection.vendor != "postgresql":
        pytest.skip("Row-lock serialization requires PostgreSQL")
    monkeypatch.setattr("repositories.approvals.enqueue", lambda pk: None)
    second = original
    if different_original:
        audit = AuditResult.objects.create(submission=original.audit.submission, generation=2, source_sha=original.source_sha,
            policy_sha=original.audit.policy_sha, detector_version="test", decision="needs_review")
        second = LicenseOriginal.objects.create(audit=audit, path=original.path, raw=original.raw, sha256=original.sha256,
            source_url=original.source_url, source_sha=original.source_sha, verified=True)
    barrier = Barrier(2)
    def submit(index):
        try:
            barrier.wait(timeout=10)
            return request_approval((original, second)[index].pk, original.audit.submission.user).pk
        finally:
            connections.close_all()
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(submit, range(2)))
    assert ids[0] == ids[1] and LicenseApproval.objects.count() == 1
