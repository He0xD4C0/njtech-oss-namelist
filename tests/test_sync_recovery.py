import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from unittest.mock import Mock, patch

import pytest
from celery.exceptions import Retry
from django.db import close_old_connections, connection, connections
from django.utils import timezone

from core.models import PolicyDocument, SourceSyncState, SyncRun
from core.runtime import get_setting, set_setting
from core.sync import OwnershipLost, SyncLease, execute_sync, queue_sync, recovery_state
from core.tasks import sync_documents

pytestmark = pytest.mark.django_db


def pending(kind="documents", status="running", seconds=-1, token=True):
    key = "policy_source_url" if kind == "documents" else "license_source_url"
    source = f"https://github.com/example/{kind}.git"
    set_setting(key, source)
    run = SyncRun.objects.create(kind=kind, status=status,
                                execution_token=uuid.uuid4() if token else None,
                                lease_expires_at=timezone.now() + timedelta(seconds=seconds) if token else None)
    return run, source, SourceSyncState.objects.get(kind=kind).generation


@pytest.mark.parametrize("kind", ["documents", "licenses"])
def test_unconfigured_direct_call_keeps_existing_result(kind):
    work = Mock()
    assert execute_sync(sync_documents, kind, work) == "not-configured"
    work.assert_not_called()


def test_business_retry_publish_failure_becomes_recoverable(monkeypatch):
    from celery.exceptions import Reject

    run, source, generation = pending()
    monkeypatch.setattr(sync_documents.request, "called_directly", False)
    with patch.object(sync_documents, "retry", side_effect=Reject("broker unavailable", requeue=False)):
        with pytest.raises(Reject):
            execute_sync(sync_documents, "documents", Mock(side_effect=ConnectionError), run.pk, source, generation)
    run.refresh_from_db()
    assert run.status == "queue-failed" and recovery_state(run) == "recoverable"


@pytest.mark.parametrize("kind", ["documents", "licenses"])
@pytest.mark.parametrize("token", [True, False])
def test_expired_and_legacy_running_tasks_resume_same_record(kind, token):
    run, source, generation = pending(kind, token=token)
    old_token = run.execution_token

    def work(lease, *_):
        assert lease.run.execution_token != old_token
        with lease.publish():
            lease.finish("success", "a" * 40)
        return "done"

    assert execute_sync(sync_documents, kind, work, run.pk, source, generation) == "done"
    run.refresh_from_db()
    assert run.status == "success" and run.execution_token is None and run.lease_expires_at is None
    assert SyncRun.objects.filter(kind=kind).count() == 2


def test_redelivery_waits_for_lease_without_spending_business_retries(monkeypatch):
    run, source, generation = pending(seconds=110)
    monkeypatch.setattr(sync_documents.request, "called_directly", False)
    monkeypatch.setattr(sync_documents.request, "delivery_info", {"redelivered": True})
    monkeypatch.setattr(sync_documents.request, "retries", 40)
    work = Mock()
    with patch.object(sync_documents, "retry", side_effect=Retry) as retry:
        with pytest.raises(Retry):
            execute_sync(sync_documents, "documents", work, run.pk, source, generation, business_retries=2)
    work.assert_not_called()
    assert 109 <= retry.call_args.kwargs["countdown"] <= 111
    assert retry.call_args.kwargs["kwargs"]["business_retries"] == 2
    assert retry.call_args.kwargs["kwargs"]["run_id"] == run.pk
    assert sync_documents.max_retries is None


def test_renewal_cannot_revive_expired_ownership():
    run, source, generation = pending(seconds=70)
    lease = SyncLease(run, source, generation)
    assert lease.renew()
    run.refresh_from_db()
    assert run.lease_expires_at > timezone.now() + timedelta(seconds=115)
    SyncRun.objects.filter(pk=run.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    assert not lease.renew() and lease.lost.is_set()
    with pytest.raises(OwnershipLost), lease.publish():
        lease.finish("success")


def test_old_executor_cannot_publish_or_fail_after_takeover():
    run, source, generation = pending()
    old = SyncLease(run, source, generation)

    def work(lease, *_):
        with pytest.raises(OwnershipLost), old.publish():
            old.finish("failed")
        with lease.publish():
            PolicyDocument.objects.create(kind="terms", language="zh-CN", body="new", source_sha="a")
            lease.finish("success", "a")

    execute_sync(sync_documents, "documents", work, run.pk, source, generation)
    with pytest.raises(OwnershipLost), old.publish():
        PolicyDocument.objects.all().delete()
    assert PolicyDocument.objects.get().body == "new"
    run.refresh_from_db()
    assert run.status == "success"


def test_lease_expiring_during_publication_rolls_back_content():
    run, source, generation = pending(seconds=100)
    lease = SyncLease(run, source, generation)
    with pytest.raises(OwnershipLost), lease.publish():
        PolicyDocument.objects.create(kind="terms", language="zh-CN", body="late", source_sha="a")
        with patch("core.sync.timezone.now", return_value=run.lease_expires_at + timedelta(seconds=1)):
            lease.finish("success")
    assert not PolicyDocument.objects.exists()


def test_heartbeat_failure_revokes_local_publication(monkeypatch):
    run, source, generation = pending(seconds=100)
    lease = SyncLease(run, source, generation)
    monkeypatch.setattr(lease.stopped, "wait", lambda _: False)
    monkeypatch.setattr(lease, "renew", Mock(side_effect=ConnectionError))
    with patch("core.sync.close_old_connections"), patch("core.sync.connections.close_all"):
        lease.heartbeat()
    assert lease.lost.is_set()
    with pytest.raises(OwnershipLost), lease.publish():
        lease.finish("success")


@pytest.mark.parametrize("status,seconds,expected", [("running", 100, "executing"), ("running", 40, "waiting"),
                                                       ("running", -1, "recoverable"), ("failed", -1, "recoverable")])
def test_read_only_recovery_states(status, seconds, expected):
    run, *_ = pending(status=status, seconds=seconds)
    assert recovery_state(run) == expected
    assert SyncRun.objects.get(pk=run.pk).status == status


def test_recovery_is_same_record_idempotent_and_retains_configuration():
    run, source, generation = pending()
    with patch("core.tasks.sync_documents.delay") as dispatch:
        assert queue_sync("documents", run.pk)[1]
        assert not queue_sync("documents", run.pk)[1]
        dispatch.assert_called_once_with(run_id=run.pk, source=source, generation=generation)
    assert get_setting("policy_source_url") == source
    assert SyncRun.objects.filter(kind="documents").count() == 2


def test_recovery_rejects_wrong_kind_active_completed_and_old_requests():
    run, source, generation = pending(seconds=100)
    with patch("core.tasks.sync_documents.delay") as dispatch:
        assert not queue_sync("documents", run.pk)[1]
        set_setting("license_source_url", "https://github.com/example/licenses.git")
        with pytest.raises(ValueError):
            queue_sync("licenses", run.pk)
        SyncRun.objects.filter(pk=run.pk).update(status="success")
        assert not queue_sync("documents", run.pk)[1]
        SyncRun.objects.filter(pk=run.pk).update(status="failed")
        set_setting("policy_source_url", "https://github.com/example/changed.git")
        assert not queue_sync("documents", run.pk)[1]
        dispatch.assert_not_called()


def test_failed_requeue_keeps_original_record_and_is_retryable():
    run, *_ = pending()
    with patch("core.tasks.sync_documents.delay", side_effect=ConnectionError):
        result, queued = queue_sync("documents", run.pk)
    assert not queued and result.pk == run.pk and result.status == "queue-failed"
    with patch("core.tasks.sync_documents.delay"):
        assert queue_sync("documents", run.pk)[1]


def test_setup_recovery_requires_authorization_and_has_read_only_status(client, settings):
    from allauth.account.models import EmailAddress

    from core.models import SiteState, User

    run, *_ = pending()
    u = User.objects.create_superuser("admin", email="recovery@example.test")
    EmailAddress.objects.create(user=u, email=u.email, primary=True, verified=True)
    SiteState.objects.create(pk=1, bootstrap_user=u)
    for key, value in {"smtp_host": "smtp.example.test", "smtp_verified": "1",
                       "turnstile_site_key": "site", "turnstile_secret_key": "secret"}.items():
        set_setting(key, value)
    data = {"action": "recover-documents", "task": "documents", "step": "requirements", "run_id": run.pk}
    with patch("core.tasks.sync_documents.delay") as dispatch:
        client.post("/setup/", data)
        dispatch.assert_not_called()
        assert client.get("/setup/?action=status").status_code == 403
        client.post("/setup/", {"action": "authorize", "secret": settings.BOOTSTRAP_SECRET})
        response = client.get("/setup/?action=status")
        assert "no-store" in response.headers["Cache-Control"]
        assert str(run.execution_token) not in response.content.decode()
        assert next(t for t in response.json()["tasks"] if t["key"] == "documents")["recovery"] == "recoverable"
        dispatch.assert_not_called()
        assert client.post("/setup/", {**data, "action": "recover-licenses"}).status_code == 400
        assert client.post("/setup/", {**data, "run_id": "invalid"}).status_code == 400
        client.post("/setup/", data)
        client.post("/setup/", data)
        dispatch.assert_called_once()


def test_runtime_recovery_requires_superuser_and_reuses_record(client, initialized_site):
    from allauth.account.models import EmailAddress

    from core.models import User

    run, *_ = pending()
    u = User.objects.create_user("operator", email="operator@example.test", password="local-test-only")
    EmailAddress.objects.create(user=u, email=u.email, primary=True, verified=True)
    client.force_login(u)
    data = {"action": "recover-documents", "run_id": run.pk}
    with patch("core.tasks.sync_documents.delay") as dispatch:
        client.post("/manage/settings/", data)
        dispatch.assert_not_called()
        u = User.objects.create_superuser("admin", password="local-test-only")
        client.force_login(u)
        client.post("/manage/settings/", data)
        client.post("/manage/settings/", data)
        dispatch.assert_called_once()
        assert dispatch.call_args.kwargs["run_id"] == run.pk


@pytest.mark.django_db(transaction=True)
def test_postgres_concurrent_claim_and_manual_recovery():
    if connection.vendor != "postgresql":
        pytest.skip("Ownership competition requires PostgreSQL")
    run, source, generation = pending()
    barrier, entered, release = Barrier(2), Event(), Event()
    executions = []

    def work(lease, *_):
        executions.append(lease.run.execution_token)
        entered.set()
        assert release.wait(10)
        with lease.publish():
            lease.finish("success")
        return "done"

    def invoke():
        close_old_connections()
        try:
            barrier.wait(10)
            return execute_sync(sync_documents, "documents", work, run.pk, source, generation)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(invoke) for _ in range(2)]
        try:
            assert entered.wait(10)
            with patch("core.tasks.sync_documents.delay") as dispatch:
                assert not queue_sync("documents", run.pk)[1]
                dispatch.assert_not_called()
        finally:
            release.set()
        results = [f.result(10) for f in futures]
    assert len(executions) == 1 and "done" in results


@pytest.mark.django_db(transaction=True)
def test_postgres_concurrent_recovery_queues_once():
    if connection.vendor != "postgresql":
        pytest.skip("Ownership competition requires PostgreSQL")
    run, *_ = pending()
    barrier = Barrier(2)

    def recover():
        close_old_connections()
        try:
            barrier.wait(10)
            return queue_sync("documents", run.pk)[1]
        finally:
            connections.close_all()

    with patch("core.tasks.sync_documents.delay") as dispatch, ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: recover(), range(2))) == [False, True]
        dispatch.assert_called_once()
