import re
from unittest.mock import patch

import pytest
from allauth.account.models import EmailAddress, EmailConfirmationHMAC
from django.core import mail
from django.test import Client
from django.utils import timezone

from core.models import AuditEvent, PolicyDocument, RuntimeSetting, SiteState, SyncRun, User
from core.runtime import get_setting, set_setting

pytestmark = pytest.mark.django_db


def authorize(client, settings):
    response = client.post("/setup/", {"action": "authorize", "secret": settings.BOOTSTRAP_SECRET})
    assert response.status_code == 302
    assert "no-store" in response["Cache-Control"]


@pytest.mark.parametrize("role", ["anonymous", "user", "admin"])
@pytest.mark.parametrize("method", ["get", "post"])
def test_all_business_entrances_closed_without_side_effects(role, method):
    client = Client(enforce_csrf_checks=True)
    if role != "anonymous":
        user = User.objects.create_user("reader", password="password", is_staff=role == "admin", is_superuser=role == "admin")
        client.force_login(user)
    counts = (User.objects.count(), RuntimeSetting.objects.count(), AuditEvent.objects.count())
    with patch("core.tasks.sync_documents.delay") as enqueue:
        for path in ("/", "/repositories/", "/rankings/", "/policies/terms/", "/accounts/login/", "/accounts/signup/", "/accounts/email/", "/admin/", "/manage/settings/", "/account/delete/", "/platforms/", "/webhooks/documents/", "/health/", "/not-a-route"):
            response = getattr(client, method)(path)
            assert response.status_code == 302, path
            assert response.url == "/setup/", path
            assert "no-store" in response["Cache-Control"]
        enqueue.assert_not_called()
    assert counts == (User.objects.count(), RuntimeSetting.objects.count(), AuditEvent.objects.count())
    assert not SiteState.objects.exists()


def test_exceptions_missing_secret_and_activation_change(client, settings):
    settings.BOOTSTRAP_SECRET = ""
    response = client.get("/setup/")
    assert response.status_code == 503 and b"BOOTSTRAP_SECRET" in response.content
    assert "no-store" in response["Cache-Control"]
    assert client.get("/accounts/confirm-email/invalid/").url == "/setup/"
    assert client.get("/static/nonexistent.css").status_code == 404
    response = client.post("/i18n/setlang/", {"language": "en", "next": "/setup/"})
    assert response.status_code == 302 and "no-store" in response["Cache-Control"]
    SiteState.objects.update(activated_at=timezone.now())
    assert client.get("/").status_code == 200
    assert client.get("/not-a-route").status_code == 404
    response = client.get("/setup/")
    assert response.status_code == 404 and "no-store" in response["Cache-Control"]


def test_groups_resume_and_blank_secret_retention(client, settings):
    authorize(client, settings)
    set_setting("smtp_host", "smtp.example.test")
    set_setting("smtp_password", "private-password")
    set_setting("smtp_verified", "1")
    response = client.post("/setup/", {"action": "configure", "step": "requirements", "group": "turnstile", "turnstile_site_key": "site", "turnstile_secret_key": "secret", "smtp_host": "attacker.invalid"})
    assert response.status_code == 302
    assert get_setting("smtp_host") == "smtp.example.test"
    assert get_setting("smtp_verified") == "1"
    assert client.post("/setup/", {"action": "configure", "step": "mail", "group": "turnstile"}).status_code == 400
    assert client.post("/setup/", {"action": "configure", "step": "requirements", "group": "turnstile", "turnstile_site_key": "site"}).status_code == 302
    assert get_setting("turnstile_secret_key") == "secret"
    assert b'value="secret"' not in client.get("/setup/?step=requirements").content
    fresh = Client()
    assert not fresh.get("/setup/?step=requirements").context["authorized"]
    fresh.post("/setup/", {"action": "configure", "step": "mail", "group": "smtp", "smtp_host": "bad.invalid"})
    assert get_setting("smtp_host") == "smtp.example.test"
    authorize(fresh, settings)
    assert fresh.get("/setup/?step=mail").context["groups"][0]["form"]["smtp_host"].value() == "smtp.example.test"
    # Changing a saved SMTP field invalidates only its own verification.
    client.post("/setup/", {"action": "configure", "step": "mail", "group": "smtp", "smtp_host": "new.example.test", "smtp_security": "starttls"})
    assert get_setting("smtp_password") == "private-password"
    assert get_setting("smtp_verified") == ""
    assert get_setting("turnstile_site_key") == "site"


def test_mail_failure_resend_cross_device_and_activation(client, settings):
    assert not client.get("/setup/?confirmed=1").context["email_confirmed"]
    authorize(client, settings)
    set_setting("smtp_host", "smtp.example.test")
    data = {"action": "account", "step": "mail", "username": "first-admin", "email": "first@example.test", "password": "Good-passphrase-427-test!"}
    with patch.object(EmailAddress, "send_confirmation", side_effect=OSError("private SMTP detail")):
        response = client.post("/setup/", data)
    assert response.status_code == 200
    assert b"private SMTP detail" not in response.content
    assert not User.objects.exists()
    assert SiteState.objects.get().bootstrap_user_id is None
    assert client.post("/setup/", data).status_code == 302
    user = User.objects.get(username="first-admin")
    with patch.object(EmailAddress, "send_confirmation", side_effect=OSError):
        assert client.post("/setup/", {"action": "resend"}).status_code == 302
    assert client.post("/setup/", {"action": "resend"}).status_code == 302
    assert len(mail.outbox) == 2
    other = User.objects.create_user("other", email="other@example.test")
    other_address = EmailAddress.objects.create(user=other, email=other.email, primary=True)
    other_path = "/accounts/confirm-email/" + EmailConfirmationHMAC(other_address).key + "/"
    device = Client()
    assert device.post(other_path).status_code == 302
    other_address.refresh_from_db()
    assert not other_address.verified
    path = re.search(r"/accounts/confirm-email/[^\s]+/", mail.outbox[-1].body).group(0)
    assert device.get(path).status_code == 200
    assert device.post(path).status_code == 302
    assert EmailAddress.objects.get(user=user).verified
    assert "bootstrap_authorization" not in device.session
    device.post("/setup/", {"action": "activate"})
    assert SiteState.objects.get().activated_at is None
    assert client.post("/setup/", {"action": "activate", "step": "review"}).status_code == 200
    for key in ("turnstile_site_key", "turnstile_secret_key"):
        set_setting(key, "test")
    for kind in ("privacy", "terms", "inclusion"):
        PolicyDocument.objects.create(kind=kind, language="en-US", body="Policy", source_sha="a" * 40)
    # The checklist is recomputed on POST, including changes since the last GET.
    assert client.get("/setup/?step=review").context["can_activate"]
    set_setting("smtp_verified", "0")
    assert client.post("/setup/", {"action": "activate"}).status_code == 200
    set_setting("smtp_verified", "1")
    response = client.post("/setup/", {"action": "activate"})
    assert response.url == "/accounts/login/"
    user.refresh_from_db()
    assert user.is_staff and user.is_superuser
    assert client.post("/setup/", {"action": "activate"}).status_code == 404
    assert AuditEvent.objects.filter(action="site.activated").count() == 1


def test_sync_queue_failure_retry_and_refresh_do_not_repeat(client, settings):
    authorize(client, settings)
    set_setting("policy_source_url", "https://github.com/example/policies.git")
    with patch("core.tasks.sync_documents.delay", side_effect=OSError("broker unavailable")) as task:
        response = client.post("/setup/", {"action": "sync-documents"}, follow=True)
        assert b"Could not queue sync" in response.content
        task.assert_called_once()
    with patch("core.tasks.sync_documents.delay") as task:
        response = client.post("/setup/", {"action": "sync-documents"}, follow=True)
        assert b"Sync queued" in response.content
        assert SyncRun.objects.filter(status="queued").count() == 1
        assert SyncRun.objects.filter(status="queue-failed").count() == 1
        SyncRun.objects.create(kind="documents", status="failed", message="source unavailable")
        assert b"source unavailable" in client.get("/setup/?step=requirements").content
        client.get("/setup/?step=requirements")
        task.assert_called_once()


def test_runtime_groups_use_same_contract(client, initialized_site):
    user = User.objects.create_superuser("admin", "admin@example.test", "password")
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    client.force_login(user)
    set_setting("smtp_host", "smtp.example.test")
    set_setting("smtp_verified", "1")
    set_setting("oauth_github_client_secret", "private-value")
    response = client.post("/manage/settings/", {"action": "save", "group": "operations", "restore_schedule": "quarterly"})
    assert response.url == "/manage/settings/#operations"
    assert get_setting("restore_schedule") == "quarterly"
    assert get_setting("smtp_host") == "smtp.example.test" and get_setting("smtp_verified") == "1"
    assert get_setting("oauth_github_client_secret") == "private-value"
    assert b"private-value" not in client.get("/manage/settings/").content


def test_backup_status_comes_from_maintenance_and_expires():
    from django.core.cache import cache

    from core.initialization import backup_status
    from core.tasks import report_backup_readiness

    cache.delete("backup-readiness")
    assert "No current maintenance status" in backup_status()
    with patch.object(cache, "set", side_effect=ConnectionError), patch("deploy.backup.readiness.readiness", return_value="ready"):
        assert report_backup_readiness() == "unavailable"
    for state, message in (("ready", "configuration ready"), ("disabled", "backups disabled"), ("not-configured", "configuration incomplete")):
        with patch("deploy.backup.readiness.readiness", return_value=state):
            assert report_backup_readiness() == state
        assert message in backup_status()
    with patch("deploy.backup.readiness.readiness", side_effect=ValueError):
        assert report_backup_readiness() == "invalid"
    assert "Invalid external backup" in backup_status()
    cache.delete("backup-readiness")
    assert "No current maintenance status" in backup_status()
