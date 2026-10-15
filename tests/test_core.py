import hashlib
import hmac
import json
from unittest.mock import patch

import pytest
from allauth.account.models import EmailAddress
from django.core.exceptions import ValidationError
from django.utils import timezone

from core.crypto import decrypt, encrypt
from core.forms import ConfigurationForm, SignupForm
from core.git import validate_git_url
from core.markdown import render_markdown
from core.models import PolicyDocument, RuntimeSetting, SiteState, User
from core.runtime import get_setting, registration_ready, set_setting

pytestmark = pytest.mark.django_db


def verified_user(**kwargs):
    user = User.objects.create_user(
        username=kwargs.pop("username", "tester"),
        email=kwargs.pop("email", "tester@example.org"),
        password="safe-user-password!443",
        **kwargs,
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    return user


def ready_site():
    SiteState.objects.create(activated_at=timezone.now())
    for key, value in {
        "smtp_host": "smtp.example.org",
        "smtp_verified": "1",
        "turnstile_site_key": "site",
        "turnstile_secret_key": "secret",
    }.items():
        set_setting(key, value)
    for kind in ("privacy", "terms", "inclusion"):
        PolicyDocument.objects.create(kind=kind, language="zh-CN", body=f"# {kind}", source_sha="a" * 40)


def test_encrypted_configuration_and_no_secret_echo(client):
    admin = verified_user(is_staff=True, is_superuser=True)
    set_setting("oauth_github_client_secret", "secret-that-must-stay-private", user=admin)
    row = RuntimeSetting.objects.get(key="oauth_github_client_secret")
    assert row.secret and "secret-that-must-stay-private" not in row.value
    assert get_setting(row.key) == "secret-that-must-stay-private"
    client.force_login(admin)
    response = client.get("/manage/settings/")
    assert response.status_code == 200
    assert b"secret-that-must-stay-private" not in response.content
    assert decrypt(encrypt("roundtrip")) == "roundtrip"


def test_registration_gate_and_missing_language(client):
    assert not registration_ready()
    ready_site()
    assert registration_ready()
    response = client.get("/policies/privacy/?language=en-US")
    assert response.status_code == 200
    assert "This language is unavailable" in response.content.decode()
    assert "# privacy" not in response.content.decode()


def test_signup_consent_requires_current_versions(rf):
    ready_site()
    data = {
        "username": "new-user",
        "email": "new@example.org",
        "password1": "A-safe-new-password!888",
        "password2": "A-safe-new-password!888",
        "policy_language": "zh-CN",
        "policy_versions": json.dumps(
            {"zh-CN": {key: "a" * 40 for key in ("terms", "privacy", "inclusion")}}
        ),
        "accept_policies": "on",
        "cf-turnstile-response": "token",
    }
    request = rf.post("/accounts/signup/", data)
    with patch("core.forms.verify_turnstile"):
        form = SignupForm(data, request=request)
        assert form.is_valid(), form.errors
    PolicyDocument.objects.filter(kind="terms").update(source_sha="b" * 40)
    with patch("core.forms.verify_turnstile"):
        assert not SignupForm(data, request=request).is_valid()


def test_username_and_safe_markdown():
    with pytest.raises(ValidationError):
        User(username="UpperCase", email="u@example.org").full_clean(exclude=["password"])
    html = str(
        render_markdown(
            "[click](javascript:alert(1))\n<script>alert(1)</script>\n<img src=x onerror=alert(1)>"
        )
    )
    assert "<script>" not in html and "<img" not in html and 'href="javascript:' not in html


def test_provider_url_rejects_arbitrary_hosts_and_credentials():
    assert validate_git_url("https://github.com/acme/policy") == "https://github.com/acme/policy.git"
    for bad in (
        "https://github.com.evil.org/acme/repo",
        "https://token@github.com/acme/repo",
        "file:///tmp/repo",
        "https://github.com/acme/repo/tree/main",
        "https://gitee.com/acme/policy",
    ):
        with pytest.raises(ValueError):
            validate_git_url(bad, github_only=True)


def test_setup_authorization_closes_permanently(client, settings):
    assert client.get("/setup/").status_code == 200
    assert client.post("/setup/", {"action": "authorize", "secret": "wrong"}).status_code == 403
    assert (
        client.post("/setup/", {"action": "authorize", "secret": settings.BOOTSTRAP_SECRET}).status_code
        == 302
    )
    SiteState.objects.update(activated_at=timezone.now())
    assert client.get("/setup/").status_code == 404


def test_bootstrap_initial_admin_requires_real_site_confirmation_flow(client, settings):
    import re

    from django.core import mail

    assert (
        client.post("/setup/", {"action": "authorize", "secret": settings.BOOTSTRAP_SECRET}).status_code
        == 302
    )
    for key, value in {
        "smtp_host": "local.invalid",
        "turnstile_site_key": "test-only",
        "turnstile_secret_key": "test-secret",
    }.items():
        set_setting(key, value)
    for kind in ("privacy", "terms", "inclusion"):
        PolicyDocument.objects.create(kind=kind, language="zh-CN", body="Policy", source_sha="a" * 40)
    response = client.post(
        "/setup/",
        {
            "action": "account",
            "username": "first-admin",
            "email": "first@example.test",
            "password": "A-strong-initial-password-527!",
        },
    )
    assert response.status_code == 302
    user = User.objects.get(username="first-admin")
    assert not user.is_staff and not user.is_superuser
    client.post("/setup/", {"action": "activate"})
    assert SiteState.objects.get(pk=1).activated_at is None
    path = re.search(r"/accounts/confirm-email/[^\s]+/", mail.outbox[-1].body).group(0)
    assert client.post(path).status_code == 302
    assert EmailAddress.objects.get(user=user).verified
    assert client.post("/setup/", {"action": "activate"}).status_code == 302
    user.refresh_from_db()
    assert user.is_staff and user.is_superuser
    assert client.get("/setup/").status_code == 404


def test_webhook_verifies_signature_repository_and_branch(client):
    set_setting("license_source_url", "https://github.com/acme/rules.git")
    set_setting("license_webhook_secret", "hook-secret")
    body = json.dumps(
        {
            "ref": "refs/heads/main",
            "repository": {"html_url": "https://github.com/acme/rules", "default_branch": "main"},
        }
    ).encode()
    signature = "sha256=" + hmac.new(b"hook-secret", body, hashlib.sha256).hexdigest()
    assert (
        client.post(
            "/webhooks/licenses/", body, content_type="application/json", HTTP_X_GITHUB_EVENT="push"
        ).status_code
        == 403
    )
    with patch("core.tasks.sync_license_policy.delay") as enqueue:
        response = client.post(
            "/webhooks/licenses/",
            body,
            content_type="application/json",
            HTTP_X_GITHUB_EVENT="push",
            HTTP_X_HUB_SIGNATURE_256=signature,
        )
        assert response.status_code == 202
        enqueue.assert_called_once()


def test_gitlab_standard_push_reads_project_default_branch(client):
    set_setting("policy_source_url", "https://gitlab.com/acme/documents.git")
    set_setting("policy_webhook_secret", "test-token")
    body = {
        "ref": "refs/heads/main",
        "project": {"web_url": "https://gitlab.com/acme/documents", "default_branch": "main"},
        "repository": {"homepage": "https://gitlab.com/acme/documents"},
    }
    with patch("core.tasks.sync_documents.delay") as enqueue:
        response = client.post(
            "/webhooks/documents/",
            json.dumps(body),
            content_type="application/json",
            HTTP_X_GITLAB_TOKEN="test-token",
            HTTP_X_GITLAB_EVENT="Push Hook",
        )
        assert response.status_code == 202
        enqueue.assert_called_once()


def test_admin_cannot_edit_runtime_and_must_have_verified_email(client):
    admin = verified_user(is_staff=True)
    client.force_login(admin)
    assert client.get("/manage/settings/").status_code == 302
    assert client.get("/admin/core/user/").status_code == 403
    EmailAddress.objects.filter(user=admin).update(verified=False)
    assert client.get("/account/profile/").url.endswith("/accounts/email/")


def test_avatar_unconfigured_is_disabled():
    from core.avatar import avatar_available, avatar_url

    assert not avatar_available()
    user = User(username="local", avatar_key="some-key")
    assert avatar_url(user).endswith("default-avatar.svg")


def test_smtp_health_is_not_reset_when_unchanged():
    from core.views import save_configuration

    set_setting("smtp_host", "smtp.example.org")
    set_setting("smtp_verified", "1")
    form = ConfigurationForm({"smtp_host": "smtp.example.org", "restore_schedule": "monthly"})
    assert form.is_valid(), form.errors
    save_configuration(form)
    assert get_setting("smtp_verified") == "1"
