from unittest.mock import patch

import pytest
from allauth.account.models import EmailAddress
from django.contrib import admin
from django.test import RequestFactory
from django.urls import reverse
from django.utils import translation

from core.models import SyncRun, User
from core.runtime import get_setting, set_setting
from repositories.models import AuditResult, Repository, Submission

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("initialized_site")]


@pytest.fixture
def manager(client):
    user = User.objects.create_superuser("admin", password="Isolated-test-password-42", language="en")
    client.force_login(user)
    return user


@pytest.fixture
def claim():
    user = User.objects.create_user("member", password="Isolated-test-password-42")
    repository = Repository.objects.create(
        platform="github", platform_id="1", full_name="member/project", name="project", owner_id="1",
        html_url="https://github.com/member/project", clone_url="https://github.com/member/project.git",
    )
    return Submission.objects.create(user=user, repository=repository, status="needs_review", reason="许可证需要处理 / License needs attention")


@pytest.mark.parametrize("path", ["/admin/", "/manage/settings/", "/admin/core/user/", "/admin/bulletins/announcement/add/"])
def test_management_is_chinese_without_changing_public_preference(client, manager, path):
    client.cookies["django_language"] = "en"
    response = client.get(path, HTTP_ACCEPT_LANGUAGE="en")
    assert response.status_code == 200
    assert response["Content-Language"] == "zh-hans"
    assert response.wsgi_request.LANGUAGE_CODE == "zh-hans"
    assert not response.context["show_languages"]
    assert '<html lang="zh-hans"' in response.content.decode()
    assert " / " not in response.context["title"]
    assert client.cookies["django_language"].value == "en"
    public = client.get("/")
    assert public["Content-Language"] == "en"
    assert 'id="ui-language"' in public.content.decode()


def test_dashboard_filters_and_search_link_to_readable_evidence(client, manager, claim):
    run = SyncRun.objects.create(kind="documents", status="queue-failed", message="任务服务不可用 / Task service unavailable")
    result = AuditResult.objects.create(
        submission=claim, generation=1, source_sha="a" * 40, policy_sha="b" * 40,
        detector_version="test", decision="needs_review", evidence={"license": "<script>unsafe()</script>"},
    )
    dashboard = client.get("/admin/")
    card = next(item for item in dashboard.context["overview_cards"] if item["title"] == "待处理申请")
    assert card["count"] == 1
    response = client.get(card["url"], {"q": "member/project"})
    assert list(response.context["cl"].queryset) == [claim]
    assert "member/project" in response.content.decode()
    assert "License needs attention" not in response.content.decode()
    detail = client.get(reverse("admin:repositories_submission_change", args=[claim.pk]))
    assert "许可证需要处理" in detail.content.decode()
    assert "License needs attention" not in detail.content.decode()
    assert reverse("admin:repositories_auditresult_change", args=[result.pk]) in detail.content.decode()
    evidence = client.get(reverse("admin:repositories_auditresult_change", args=[result.pk])).content.decode()
    assert "&lt;script&gt;" in evidence and "<script>unsafe()" not in evidence
    failures = client.get("/admin/core/syncrun/?status=failed")
    assert list(failures.context["cl"].queryset) == [run]
    for path in ("/admin/core/syncrun/", reverse("admin:core_syncrun_change", args=[run.pk]), "/manage/settings/"):
        content = client.get(path).content.decode()
        assert "任务服务不可用" in content and "Task service unavailable" not in content
    claim.refresh_from_db()
    run.refresh_from_db()
    assert claim.reason == "许可证需要处理 / License needs attention"
    assert run.message == "任务服务不可用 / Task service unavailable"


def test_staff_has_business_access_without_private_configuration(client, claim):
    user = claim.user
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    EmailAddress.objects.create(user=user, email="member@example.test", verified=True, primary=True)
    client.force_login(user)
    response = client.get("/admin/")
    assert response.status_code == 200
    assert "失败任务" not in [item["title"] for item in response.context["overview_cards"]]
    assert b"/manage/settings/" not in response.content
    assert client.get("/manage/settings/").status_code == 302
    assert client.get("/admin/core/syncrun/").status_code == 403
    for model in (Submission, Repository, AuditResult):
        model_admin = admin.site._registry[model]
        request = RequestFactory().get("/admin/")
        request.user = user
        assert model_admin.has_view_permission(request)
        assert not model_admin.has_change_permission(request)
        assert not model_admin.has_add_permission(request)
        assert not model_admin.has_delete_permission(request)


def test_recheck_preserves_status_and_skips_removed_claims(client, manager, claim):
    removed = Submission.objects.create(user=claim.user, repository=claim.repository, status="removed")
    with patch("repositories.tasks.schedule_audit", return_value=True) as schedule:
        response = client.post("/admin/repositories/submission/", {
            "action": "recheck", "_selected_action": [claim.pk, removed.pk],
        }, follow=True)
    schedule.assert_called_once_with(claim.pk)
    assert "已为 1 条申请安排重新机审" in response.content.decode()
    claim.refresh_from_db()
    assert claim.status == "needs_review"


def test_save_and_test_mail_uses_submitted_values_and_keeps_secrets_private(client, manager):
    set_setting("smtp_host", "old.example.test")
    set_setting("smtp_password", "existing-private-password")
    data = {"action": "test-smtp", "group": "smtp", "smtp_host": "new.example.test", "smtp_port": "587",
            "smtp_security": "starttls", "smtp_username": "sender@example.test", "smtp_from_email": "sender@example.test",
            "smtp_password": "", "recipient": "recipient@example.test"}
    seen = []
    with patch("core.views.send_smtp_test", side_effect=lambda recipient: seen.append((recipient, get_setting("smtp_host")))):
        response = client.post("/manage/settings/", data)
    assert response.url == "/manage/settings/#smtp"
    assert seen == [("recipient@example.test", "new.example.test")]
    assert get_setting("smtp_verified") == "1"
    assert get_setting("smtp_password") == "existing-private-password"
    response = client.get(response.url)
    assert b"existing-private-password" not in response.content
    with patch("core.views.send_smtp_test") as send:
        response = client.post("/manage/settings/", {**data, "smtp_host": "invalid://host", "recipient": "invalid"})
    assert response.status_code == 200
    assert response.context["selected_group"] == "smtp"
    assert response.context["recipient_form"].errors
    assert response.context["groups"][0]["form"].errors
    assert get_setting("smtp_host") == "new.example.test"
    send.assert_not_called()


def test_save_and_test_avatar_uses_submitted_values(client, manager):
    seen = []
    with patch("core.avatar.test_storage", side_effect=lambda: seen.append(get_setting("s3_bucket"))):
        response = client.post("/manage/settings/", {
            "action": "test-s3", "group": "storage", "s3_endpoint": "https://storage.example.test",
            "s3_bucket": "new-bucket", "s3_prefix": "avatars", "s3_region": "auto",
            "s3_access_key": "test-access", "s3_secret_key": "private-secret",
        })
    assert response.url == "/manage/settings/#storage"
    assert seen == ["new-bucket"] and get_setting("s3_verified") == "1"
    with patch("core.avatar.test_storage") as test:
        response = client.post("/manage/settings/", {"action": "test-s3", "group": "storage", "s3_bucket": "https://bad"})
    assert response.status_code == 200
    assert get_setting("s3_bucket") == "new-bucket"
    test.assert_not_called()


def test_unknown_configuration_actions_do_not_change_settings(client, manager):
    for data in ({"action": "unknown"}, {"action": "save", "group": "unknown"},
                 {"action": "test-smtp", "group": "storage", "s3_bucket": "bad"}):
        assert client.post("/manage/settings/", data).status_code == 400
    assert not get_setting("s3_bucket")
    with translation.override("en"):
        assert User._meta.get_field("language").choices[-1] == ("en", "English")
