import hashlib
import json
import subprocess
import uuid
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.core.cache import cache
from django.utils import timezone

from core.backup import candidate, public_status, queue_operation, save_candidate
from core.backup_forms import BackupConfigurationForm
from core.models import BackupConfiguration, SiteState, SyncRun, User
from deploy.backup import managed

pytestmark = pytest.mark.django_db
VALUES = {"endpoint": "backup.example.invalid", "bucket": "database-backups", "region": "auto",
          "access_key": "test-access", "secret_key": "test-secret", "uri_style": "path"}


@pytest.fixture
def backup_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKUP_CONFIG_MODE", "application")
    monkeypatch.setattr(managed, "ROOT", tmp_path / "config")
    monkeypatch.setattr(managed, "LOCK_ROOT", tmp_path / "locks")
    managed.initialize()
    cache.set("backup-readiness", {"checked_at": timezone.now().isoformat(), "state": "not-configured"})


def test_save_encrypts_and_retains_secrets_and_invalidates_test(backup_mode):
    config = save_candidate(VALUES)
    assert config.values["secret_key"] != VALUES["secret_key"]
    config.tested_revision = config.revision
    config.save()
    before = config.revision
    config = save_candidate(VALUES | {"secret_key": "", "access_key": ""})
    assert config.revision == before and config.tested_revision == before
    config = save_candidate(VALUES | {"secret_key": "changed"})
    assert config.revision != before and config.tested_revision is None
    assert candidate(config)["secret_key"] == "changed"


def test_enabled_location_immutable_but_credentials_can_change(backup_mode):
    config = save_candidate(VALUES)
    config.location = {k: VALUES[k] for k in ("endpoint", "bucket", "region", "uri_style")}
    config.applied_revision = config.revision
    config.save()
    with pytest.raises(ValueError):
        save_candidate(VALUES | {"bucket": "another-bucket"})
    assert candidate(save_candidate(VALUES | {"secret_key": "rotated"}))["secret_key"] == "rotated"
    form = BackupConfigurationForm(VALUES | {"bucket": "malicious-change"})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["bucket"] == VALUES["bucket"]


@pytest.mark.parametrize("endpoint", ["http://storage.example", "https://storage.example/bucket", "https://user:pass@storage.example", "https://storage.example/?key=secret", "https://storage.example/#secret", "https://storage.example:bad"])
def test_endpoint_errors_are_local_to_field(backup_mode, endpoint):
    form = BackupConfigurationForm(VALUES | {"endpoint": endpoint})
    assert not form.is_valid()
    assert "endpoint" in form.errors


def test_endpoint_normalized_and_no_secret_echo(backup_mode):
    save_candidate(VALUES)
    form = BackupConfigurationForm(VALUES | {"endpoint": "https://ACCOUNT.r2.cloudflarestorage.com/"})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["endpoint"] == "account.r2.cloudflarestorage.com"
    assert 'value="test-secret"' not in form.as_p()


def test_queue_deduplicates_and_rejects_apply_without_current_test(backup_mode):
    config = save_candidate(VALUES)
    with pytest.raises(ValueError):
        queue_operation("apply")
    with patch("core.backup.backup_operation.apply_async") as send:
        first = queue_operation("test")
        second = queue_operation("test")
        assert first.pk == second.pk
        send.assert_called_once_with(args=[first.pk], queue="maintenance")
    config.refresh_from_db()
    assert config.applied_revision is None


def test_queue_failure_can_retry_and_expired_run_cannot_execute(backup_mode):
    save_candidate(VALUES)
    with patch("core.backup.backup_operation.apply_async", side_effect=ConnectionError):
        first = queue_operation("test")
    assert first.status == "queue-failed"
    with patch("core.backup.backup_operation.apply_async"):
        second = queue_operation("test")
        second.lease_expires_at = timezone.now() - timedelta(seconds=1)
        second.save()
        third = queue_operation("test")
    assert third.pk != second.pk
    second.refresh_from_db()
    assert second.status == "failed"
    from core.backup import backup_operation
    assert backup_operation.run(second.pk) == "failed"


def test_public_status_contains_no_credentials_or_execution_token(backup_mode):
    save_candidate(VALUES)
    with patch("core.backup.backup_operation.apply_async"):
        run = queue_operation("test")
    run.execution_token = uuid.uuid4()
    run.save()
    result = json.dumps(public_status())
    for secret in (VALUES["secret_key"], VALUES["access_key"], str(run.execution_token)):
        assert secret not in result
    cache.clear()
    assert public_status()["state"] == "unavailable"


def test_s3_test_checks_all_permissions_and_cleans_only_own_key(backup_mode):
    from core.backup import test_storage
    client = Mock()
    captured = {}
    def put(**kwargs):
        captured.update(kwargs)
    client.put_object.side_effect = put
    client.get_object.side_effect = lambda **kw: {"Body": Mock(read=lambda: captured["Body"])}
    client.list_objects_v2.side_effect = lambda **kw: {"Contents": [{"Key": captured["Key"]}]}
    with patch("boto3.client", return_value=client):
        test_storage(VALUES, "/namelist/test/instance")
    client.delete_object.assert_called_once_with(Bucket=VALUES["bucket"], Key=captured["Key"])
    assert captured["Key"].startswith("namelist/test/instance/.connection-test/")


def test_stale_candidate_does_not_get_marked_tested(backup_mode):
    from core.backup import backup_operation
    save_candidate(VALUES)
    with patch("core.backup.backup_operation.apply_async"):
        run = queue_operation("test")
    with patch("core.backup.test_storage", side_effect=lambda *_: save_candidate(VALUES | {"secret_key": "new"})):
        assert backup_operation.run(run.pk) == "superseded"
    assert BackupConfiguration.objects.get().tested_revision is None


def test_successful_test_then_apply_keeps_cipher_on_retry(backup_mode):
    from core.backup import backup_operation
    config = save_candidate(VALUES)
    config.tested_revision = config.revision
    config.save()
    with patch("core.backup.backup_operation.apply_async"):
        run = queue_operation("apply")
    with patch.object(managed, "command", side_effect=subprocess.CalledProcessError(1, "pgbackrest")):
        assert backup_operation.run(run.pk) == "failed"
    original = json.loads((managed.ROOT / "repository.json").read_text())["cipher"]
    with patch("core.backup.backup_operation.apply_async"):
        retry = queue_operation("apply")
    with patch.object(managed, "command"), patch.object(managed, "backup"):
        assert backup_operation.run(retry.pk) == "success"
    assert managed.active()["cipher"] == original
    assert managed.config_path(retry.source_sha).stat().st_mode & 0o777 == 0o600


def test_missing_active_config_never_reinitializes_disabled(backup_mode):
    revision = str(uuid.uuid4())
    managed.store_config(revision, "configuration")
    managed.activate({"state": "active", "revision": revision, "values": VALUES})
    (managed.ROOT / "active.json").unlink()
    managed.initialize()
    with pytest.raises(FileNotFoundError):
        managed.active()
    managed.write_json(managed.ROOT / "active.json", {"state": "disabled"})
    with pytest.raises(ValueError):
        managed.active()


def test_bootstrap_and_admin_authorization(client, backup_mode):
    SiteState.objects.update_or_create(pk=1, defaults={"activated_at": None})
    with patch("core.backup.backup_operation.apply_async") as send:
        assert client.post("/setup/", {"step": "operations", "task": "backup", "action": "backup-test", **VALUES}).status_code == 200
        send.assert_not_called()
    assert not BackupConfiguration.objects.exists()
    SiteState.objects.update_or_create(pk=1, defaults={"activated_at": timezone.now()})
    user = User.objects.create_user(username="ordinary", email="ordinary@example.invalid", password="password")
    client.force_login(user)
    assert client.post("/manage/settings/", {"group": "backup", "action": "backup-test", **VALUES}).status_code == 302
    assert not BackupConfiguration.objects.exists()


def test_setup_can_skip_backup_and_status_is_read_only(client, backup_mode, settings):
    from allauth.account.models import EmailAddress

    from core.models import PolicyDocument
    from core.runtime import set_setting
    user = User.objects.create_superuser(username="admin", email="initial@example.invalid")
    EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    SiteState.objects.update_or_create(pk=1, defaults={"activated_at": None, "bootstrap_user": user})
    for key, value in {"smtp_host": "mail.invalid", "smtp_verified": "1", "turnstile_site_key": "key", "turnstile_secret_key": "secret"}.items():
        set_setting(key, value)
    for kind in ("privacy", "terms", "inclusion"):
        PolicyDocument.objects.create(kind=kind, language="zh-CN", body="policy")
    session = client.session
    session["bootstrap_authorization"] = hashlib.sha256(settings.BOOTSTRAP_SECRET.encode()).hexdigest()
    session.save()
    response = client.get("/setup/?step=operations&task=backup")
    assert response.status_code == 200
    assert "数据库备份" in response.content.decode()
    with patch("core.backup.backup_operation.apply_async") as send:
        assert client.get("/setup/?action=status").json()["backup"]["state"] == "not-configured"
        send.assert_not_called()
    assert client.post("/setup/", {"step": "operations", "task": "backup", "action": "skip"}).status_code == 302
    assert client.post("/setup/", {"step": "review", "task": "review", "action": "activate"}).status_code == 302
    assert SiteState.objects.get().activated_at


@pytest.mark.parametrize("origin,allowed", [("http://10.126.126.4:28000", True), ("https://10.126.126.4", False), ("http://8.8.8.8", False), ("http://example.com", False)])
def test_private_http_is_explicit_and_confined(origin, allowed):
    import os
    import sys
    env = os.environ | {"PRIVATE_HTTP": "1", "SITE_BASE_URL": origin, "SITE_ENVIRONMENT": "production", "DJANGO_DEBUG": "0", "STAGING_HTTP": "0"}
    result = subprocess.run([sys.executable, "-c", "from config import settings as s; assert not s.SESSION_COOKIE_SECURE; assert s.SESSION_COOKIE_NAME != 'sessionid'"], env=env, capture_output=True)
    assert (result.returncode == 0) == allowed


@pytest.mark.django_db(transaction=True)
def test_postgres_concurrent_backup_clicks_publish_once(backup_mode):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from django.db import close_old_connections, connection
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-lock acceptance")
    save_candidate(VALUES)
    barrier = threading.Barrier(2)
    def submit():
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            return queue_operation("test").pk
        finally:
            close_old_connections()
    with patch("core.backup.backup_operation.apply_async") as send, ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        assert len({future.result(timeout=10) for future in futures}) == 1
        assert send.call_count == 1


def test_expired_executor_cannot_enable_archive(backup_mode):
    from core.backup import backup_operation
    config = save_candidate(VALUES)
    config.tested_revision = config.revision
    config.save()
    with patch("core.backup.backup_operation.apply_async"):
        run = queue_operation("apply")
    def lose_lease(*args, **kwargs):
        SyncRun.objects.filter(pk=run.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    with patch.object(managed, "command", side_effect=lose_lease), patch.object(managed, "backup") as backup:
        assert backup_operation.run(run.pk) == "superseded"
        backup.assert_not_called()
    assert managed.active()["state"] == "disabled"


def test_scheduled_backup_busy_and_failure_are_visible(backup_mode):
    config = save_candidate(VALUES)
    config.applied_revision = config.revision
    config.save()
    cache.set("backup-readiness", {"checked_at": timezone.now().isoformat(), "backup": {"state": "running", "busy": True}})
    assert public_status()["state"] == "backuping"
    assert public_status()["busy"] is True
    cache.set("backup-readiness", {"checked_at": timezone.now().isoformat(), "backup": {"state": "failed", "busy": False, "error": "backup-failed", "last_success": "yesterday"}})
    assert public_status()["state"] == "failed"
    assert public_status()["last_success"] == "yesterday"
    assert public_status()["error"]
