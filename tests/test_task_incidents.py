from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

import httpx
import pytest
from django.db import connection, connections
from django.utils import timezone

from core.crypto import decrypt, encrypt
from core.incidents import daily_digest, deliver_notification, report, report_provider, resolve
from core.models import Incident, NotificationDelivery, User
from core.runtime import set_setting
from core.signals import report_task_failure
from providers.clients import GitHubClient, ProviderUnavailable
from providers.models import OAuthBinding
from repositories.models import AuditDispatch, Repository, Submission
from repositories.tasks import audit_submission, recover_audits, resume_authorized

pytestmark = pytest.mark.django_db


@pytest.fixture
def account():
    user = User.objects.create_user("owner")
    binding = OAuthBinding.objects.create(user=user, platform="github", platform_user_id="7", username="owner",
        encrypted_access_token=encrypt("old-access"), encrypted_refresh_token=encrypt("old-refresh"))
    set_setting("oauth_github_client_id", "client")
    set_setting("oauth_github_client_secret", "secret")
    set_setting("operations_emails", "operator@example.test")
    return binding


def claim(binding, number=1):
    repo = Repository.objects.create(platform="github", platform_id=str(number), full_name=f"owner/project-{number}", owner_id="7")
    return Submission.objects.create(user_id=binding.user_id, repository=repo)


def test_github_401_refreshes_once_and_reuses_rotated_credentials(account):
    calls = []
    def transport(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"access_token": "rotated", "refresh_token": "new-refresh", "expires_in": 28800})
        if request.headers["authorization"] == "Bearer old-access":
            return httpx.Response(401)
        return httpx.Response(200, json={"id": 7, "login": "owner"})
    client = GitHubClient(account, transport=httpx.MockTransport(transport))
    assert client.identity().id == "7"
    # Another request received a 401 before the first request finished refreshing.
    assert client._access_token(refresh=True, rejected_token="old-access") == "rotated"
    assert sum(request.method == "POST" for request in calls) == 1
    account.refresh_from_db()
    assert decrypt(account.encrypted_refresh_token) == "new-refresh"


@pytest.mark.parametrize("status,payload,code,retryable", [
    (200, {"error": "bad_refresh_token"}, "reauthorization", False),
    (401, {"error": "invalid_client"}, "configuration", False),
    (429, {}, "rate_limit", True), (503, {}, "temporary", True),
    (403, {"message": "You have exceeded a secondary rate limit."}, "rate_limit", True),
])
def test_refresh_failure_classes_preserve_credentials(account, status, payload, code, retryable):
    account.expires_at = timezone.now() - timedelta(seconds=1)
    account.save()
    token = account.encrypted_access_token
    with pytest.raises(ProviderUnavailable) as error:
        GitHubClient(account, transport=httpx.MockTransport(lambda _: httpx.Response(status, json=payload))).identity()
    assert error.value.code == code and error.value.retryable == retryable
    account.refresh_from_db()
    assert account.encrypted_access_token == token


def test_rate_limit_server_delay_is_preserved(account):
    from providers.clients import response_failure
    response = httpx.Response(403, headers={"Retry-After": "600"})
    error = response_failure(response)
    assert error.retry_after == 600 and error.code == "rate_limit"
    submission = claim(account)
    now = timezone.now()
    with patch("repositories.tasks.execute_audit", side_effect=error):
        assert audit_submission(submission.pk, 1) == "retrying"
    assert AuditDispatch.objects.get(submission=submission).retry_at >= now + timedelta(seconds=600)


@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_401s_rotate_only_once(account):
    if connection.vendor != "postgresql":
        pytest.skip("Row-lock serialization requires PostgreSQL")
    barrier, refreshes = Barrier(2), []
    def transport(request):
        if request.method == "POST":
            refreshes.append(request)
            return httpx.Response(200, json={"access_token": "rotated", "refresh_token": "new", "expires_in": 28800})
        if request.headers["authorization"] == "Bearer old-access":
            barrier.wait(timeout=10)
            return httpx.Response(401)
        return httpx.Response(200, json={"id": 7, "login": "owner"})
    def worker(_):
        try:
            return GitHubClient(account, transport=httpx.MockTransport(transport)).identity().id
        finally:
            connections.close_all()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(worker, range(2))) == ["7", "7"]
    assert len(refreshes) == 1


def test_auth_faults_wait_and_reconnect_only_current_batch_once(account):
    submission = claim(account)
    error = ProviderUnavailable("private text must not leak", code="reauthorization", retryable=False)
    with patch("repositories.tasks.execute_audit", side_effect=error):
        assert audit_submission(submission.pk, 1) == "waiting_auth"
    assert recover_audits() == 0
    job = AuditDispatch.objects.get(submission=submission)
    assert "private text" not in job.reason
    with patch("repositories.tasks.dispatch_audit") as dispatch:
        assert resume_authorized(account.user_id, "github") == 1
        assert resume_authorized(account.user_id, "github") == 0
        # The test transaction has not committed; no broker side effect yet.
        dispatch.assert_not_called()
    assert job.submission.reason == ""


def test_retry_exhaustion_and_lost_consumer_are_bounded(account):
    submission = claim(account)
    with patch("repositories.tasks.execute_audit", side_effect=ConnectionError("private")) as work:
        for attempt in range(7):
            AuditDispatch.objects.filter(submission=submission).update(retry_at=None)
            result = audit_submission(submission.pk, 1)
        assert result == "failed" and work.call_count == 7
        assert recover_audits() == 0
        assert audit_submission(submission.pk, 1) == "failed"
        assert work.call_count == 7
    second = claim(account, 2)
    job = AuditDispatch.objects.create(submission=second, generation=1, status="running", attempts=1,
        lease_expires_at=timezone.now() - timedelta(seconds=1))
    with patch("repositories.tasks.audit_submission.delay") as deliver:
        assert recover_audits() == 1
        assert recover_audits() == 0
        deliver.assert_called_once_with(second.pk, 1)
    job.refresh_from_db()
    assert job.attempts == 1


def test_duplicate_delivery_cannot_execute_with_live_lease(account):
    submission = claim(account)
    AuditDispatch.objects.create(submission=submission, generation=1, status="running", attempts=1,
        lease_expires_at=timezone.now() + timedelta(minutes=1))
    with patch("repositories.tasks.execute_audit") as work:
        assert audit_submission(submission.pk, 1) == "running"
        work.assert_not_called()


def test_incident_first_digest_recovery_and_smtp_failure_are_durable(account):
    claim(account)
    claim(account, 2)
    error = ProviderUnavailable("not retained", code="reauthorization", retryable=False)
    first = report_provider(account, error)
    report_provider(account, error)
    assert Incident.objects.count() == 1 and len(first.affected) == 2
    assert NotificationDelivery.objects.count() == 1
    delivery = NotificationDelivery.objects.get(kind="first")
    with patch("core.incidents.send_system_mail", side_effect=OSError("SMTP password private")):
        assert deliver_notification(delivery.pk) == "failed"
    delivery.refresh_from_db()
    assert "private" not in delivery.reason and delivery.attempts == 1
    NotificationDelivery.objects.filter(pk=delivery.pk).update(retry_at=None)
    with patch("core.incidents.send_system_mail", return_value=1) as send:
        assert deliver_notification(delivery.pk) == "sent"
        assert deliver_notification(delivery.pk) == "sent"
        assert send.call_count == 1
    assert daily_digest() == daily_digest()
    assert NotificationDelivery.objects.filter(kind="digest").count() == 1
    digest = NotificationDelivery.objects.get(kind="digest")
    resolve(first.key)
    resolve(first.key)
    assert daily_digest() == "empty"
    assert NotificationDelivery.objects.filter(kind="recovery").count() == 1
    with patch("core.incidents.send_system_mail") as send:
        assert deliver_notification(digest.pk) == "skipped"
        send.assert_not_called()


def test_global_hook_and_business_fault_share_one_incident():
    from core.tasks import sync_license_policy
    report("task:licenses", "licenses", "策略同步失败")
    report_task_failure(sender=sync_license_policy, exception=ValueError("secret"))
    assert Incident.objects.count() == NotificationDelivery.objects.count() == 1
