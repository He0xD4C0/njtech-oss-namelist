from datetime import timedelta

import pytest
from allauth.account.models import EmailAddress
from django.contrib.admin.models import LogEntry
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from bulletins.models import Announcement, AnnouncementReceipt
from bulletins.services import pending_announcements
from core.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def reader():
    return User.objects.create_user("reader", email="reader@example.com", password="Safe-testing-password-42")


@pytest.fixture
def staff():
    user = User.objects.create_user(
        "admin-user", email="admin@example.com", password="Safe-testing-password-42", is_staff=True
    )
    EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    return user


@pytest.fixture
def notice(staff):
    return Announcement.objects.create(
        title="Hello",
        body="**Public** announcement",
        creator=staff,
        creator_name=staff.get_full_name(),
        editors=[{"id": staff.pk, "name": staff.get_full_name()}],
    )


def receipt(client, notice, action="confirm", revision=None):
    return client.post(
        reverse("bulletins:receipt", args=[notice.pk]),
        {"action": action, "revision": notice.revision if revision is None else revision},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )


def test_normal_cross_device_dismiss_never_resent(client, reader, notice):
    client.force_login(reader)
    assert pending_announcements(reader) == [notice]
    assert receipt(client, notice, "dismiss").status_code == 200
    other = Client()
    other.force_login(reader)
    assert not other.get("/").context["pending_announcements"]
    notice.body = "Changed normal notice"
    notice.save()
    assert notice.revision == 2
    assert not pending_announcements(reader)
    assert AnnouncementReceipt.objects.count() == 1


def test_policy_reconfirm_only_content_change_and_no_close(client, reader, notice):
    notice.kind = "policy"
    notice.save()
    client.force_login(reader)
    assert receipt(client, notice, "dismiss").status_code == 400
    assert receipt(client, notice).status_code == 200
    assert not pending_announcements(reader)
    notice.expires_at = timezone.now() + timedelta(days=1)
    notice.save()
    assert notice.revision == 1 and not pending_announcements(reader)
    notice.title = "Updated title"
    notice.save()
    assert notice.revision == 2 and len(pending_announcements(reader)) == 1
    assert receipt(client, notice, revision=1).status_code == 409
    assert receipt(client, notice).status_code == 200
    notice.expires_at = timezone.now() - timedelta(seconds=1)
    notice.save()
    assert not pending_announcements(reader)
    assert receipt(client, notice).status_code == 410
    assert "已过期" in client.get(reverse("bulletins:detail", args=[notice.pk])).content.decode()


def test_creation_cutoff_is_fixed_and_future_audience(reader, notice):
    notice.include_future_users = False
    notice.save()
    future = User.objects.create_user("future", email="future@example.com")
    assert not pending_announcements(future)
    assert pending_announcements(reader)
    created = notice.created_at
    notice.title = "Edited later"
    notice.created_at = timezone.now() + timedelta(days=1)
    notice.save()
    assert notice.created_at == created and not pending_announcements(future)
    notice.include_future_users = True
    notice.save()
    assert pending_announcements(future)


def test_anonymous_read_only_csrf_and_revision_validation(client, reader, notice):
    assert client.get(reverse("bulletins:detail", args=[notice.pk])).status_code == 200
    assert receipt(client, notice).status_code == 302
    client.force_login(reader)
    assert client.get(reverse("bulletins:receipt", args=[notice.pk])).status_code == 405
    assert receipt(client, notice, revision="bad").status_code == 400
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(reader)
    assert receipt(csrf_client, notice).status_code == 403


def test_markdown_sanitized_names_preserved_and_no_old_body(client, reader, notice):
    notice.body = '<script>alert("xss")</script> [bad](javascript:alert(1)) **safe**'
    notice.record_editor(reader)
    notice.save()
    response = client.get(reverse("bulletins:detail", args=[notice.pk]))
    assert b"<script>alert" not in response.content
    assert b'href="javascript:' not in response.content
    assert b"<strong>safe</strong>" in response.content
    reader.delete()
    notice.refresh_from_db()
    assert any(item["name"] == "reader" for item in notice.editors)
    assert not any(field.name in {"draft", "previous_body", "history"} for field in notice._meta.fields)


def test_admin_is_immediate_attributed_and_has_no_log_history(client, staff, reader):
    client.force_login(reader)
    url = reverse("admin:bulletins_announcement_add")
    assert client.get(url).status_code == 302
    client.force_login(staff)
    response = client.post(
        url,
        {
            "title": "Admin notice",
            "body": "New body",
            "kind": "normal",
            "include_future_users": "on",
            "_save": "Save",
        },
    )
    assert response.status_code == 302
    item = Announcement.objects.get(title="Admin notice")
    assert item.creator_name == staff.get_full_name() and item.editors[0]["id"] == staff.pk
    assert client.get(reverse("bulletins:detail", args=[item.pk])).status_code == 200
    assert not LogEntry.objects.filter(object_id=str(item.pk), content_type__app_label="bulletins").exists()
    assert client.get(reverse("admin:bulletins_announcement_history", args=[item.pk])).status_code == 404
    item.kind = "policy"
    item.save()
    assert b"announcement-dialog" in client.get("/admin/").content


def test_policy_priority_and_all_authenticated_pages(client, reader, notice):
    policy = Announcement.objects.create(title="Policy", body="Read me", kind="policy", creator_name="admin")
    client.force_login(reader)
    assert pending_announcements(reader)[0] == policy
    for path in ("/", "/repositories/", "/rankings/", "/announcements/"):
        response = client.get(path)
        assert b'data-announcement-kind="policy"' in response.content
        assert b'"revision" value="1"' in response.content


def test_partial_expiry_edit_does_not_reconfirm_newer_body(notice):
    stale = Announcement.objects.get(pk=notice.pk)
    notice.body = "Newer body"
    notice.save(update_fields=["body"])
    assert notice.revision == 2
    stale.expires_at = timezone.now() + timedelta(days=2)
    stale.save(update_fields=["expires_at"])
    notice.refresh_from_db()
    assert notice.body == "Newer body" and notice.revision == 2
