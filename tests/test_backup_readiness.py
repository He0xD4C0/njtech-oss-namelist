from unittest.mock import patch

import pytest
from django.core.cache import cache

from core.forms import ConfigurationForm
from core.models import SyncRun
from core.runtime import get_setting
from core.tasks import restore_drill
from core.views import save_configuration

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("schedule", ["weekly", "monthly", "quarterly"])
def test_gui_frequency_cannot_bypass_unconfigured_backup(schedule, monkeypatch):
    monkeypatch.setenv("BACKUP_ENABLED", "1")
    monkeypatch.setenv("SITE_ENVIRONMENT", "staging")
    monkeypatch.setenv("BACKUP_S3_BUCKET", "")
    monkeypatch.setenv("BACKUP_CIPHER_FILE", "/missing/backup-cipher")
    form = ConfigurationForm({"restore_schedule": schedule})
    assert form.is_valid(), form.errors
    save_configuration(form)
    assert get_setting("restore_schedule") == schedule
    assert "需要服务器的独立备份配置先就绪" in str(form)
    with (
        patch("core.tasks.subprocess.run") as run,
        patch("core.tasks.record_failure") as failure,
        patch.object(cache, "add") as lock,
    ):
        assert restore_drill() == "not-configured"
        run.assert_not_called()
        failure.assert_not_called()
        lock.assert_not_called()
    assert not SyncRun.objects.filter(kind="restore").exists()


def test_disabled_backup_cannot_be_enabled_by_gui(monkeypatch):
    monkeypatch.setenv("BACKUP_ENABLED", "0")
    monkeypatch.setenv("SITE_ENVIRONMENT", "staging")
    with patch("core.tasks.subprocess.run") as run, patch("core.tasks.record_failure") as failure:
        assert restore_drill() == "disabled"
        run.assert_not_called()
        failure.assert_not_called()


@pytest.mark.parametrize("reason", ["network unavailable", "authentication failed"])
def test_configured_backup_failures_are_reported(reason):
    with (
        patch("deploy.backup.readiness.readiness", return_value="ready"),
        patch("core.tasks.subprocess.run", side_effect=OSError(reason)),
        patch("core.tasks.notify_failure.delay") as notify,
    ):
        assert restore_drill() == "failed"
        assert SyncRun.objects.filter(kind="restore", status="failed").exists()
        notify.assert_called_once_with("restore")
    assert not cache.get("restore-drill-running")


def test_invalid_configuration_is_reported_without_starting_restore():
    with (
        patch("deploy.backup.readiness.readiness", side_effect=ValueError("private sentinel")),
        patch("core.tasks.subprocess.run") as run,
        patch("core.tasks.notify_failure.delay") as notify,
    ):
        assert restore_drill() == "failed"
        run.assert_not_called()
        notify.assert_called_once_with("restore")
    assert "private sentinel" not in SyncRun.objects.get(kind="restore").message
