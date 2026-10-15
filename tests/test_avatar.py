import io
from unittest.mock import Mock, patch

import pytest
from django.core.cache import cache
from PIL import Image

from core.avatar import avatar_available, avatar_url, normalized_avatar, upload_avatar
from core.models import PendingAvatarDeletion, User
from core.runtime import set_setting
from core.services import import_oauth_profile

pytestmark = pytest.mark.django_db


def image_bytes():
    out = io.BytesIO()
    Image.new("RGB", (800, 800), "blue").save(out, "PNG")
    return out.getvalue()


def configure_s3():
    for key, value in {
        "s3_bucket": "avatar-test",
        "s3_access_key": "test-access",
        "s3_secret_key": "test-secret",
        "s3_verified": "1",
    }.items():
        set_setting(key, value)


def test_health_failure_fallback_then_recovery_preserves_existing_image():
    configure_s3()
    user = User.objects.create_user(
        username="avatar-user", email="avatar@example.test", avatar_key="existing.webp"
    )
    backend = Mock()
    backend.url.return_value = "https://example.invalid/signed-avatar"
    backend.connection.meta.client.head_bucket.side_effect = OSError("unavailable")
    with patch("core.avatar.storage", return_value=backend):
        assert not avatar_available()
        assert avatar_url(user).endswith("default-avatar.svg")
        cache.delete("avatar-storage-health")
        backend.connection.meta.client.head_bucket.side_effect = None
        assert avatar_url(user) == backend.url.return_value
    user.refresh_from_db()
    assert user.avatar_key == "existing.webp"


def test_avatar_real_pillow_reencodes_and_strips_metadata():
    output = normalized_avatar(image_bytes())
    with Image.open(io.BytesIO(output)) as image:
        assert image.format == "WEBP" and image.size == (512, 512)
        assert not image.getexif()
    with pytest.raises(ValueError):
        normalized_avatar(b"<svg><script>bad()</script></svg>")


def test_user_upload_wins_over_stale_oauth_import():
    user = User.objects.create_user(
        username="avatar-user", email="avatar@example.test", avatar_revision=1, avatar_key="uploaded.webp"
    )
    backend = Mock()
    backend.save.return_value = "new-import.webp"
    with (
        patch("core.avatar.avatar_available", return_value=True),
        patch("core.avatar.storage", return_value=backend),
    ):
        assert upload_avatar(user, image_bytes(), import_revision=0) is False
    assert PendingAvatarDeletion.objects.filter(key="new-import.webp").exists()
    user.refresh_from_db()
    assert user.avatar_key == "uploaded.webp"


def test_oauth_name_populated_independently_without_s3_and_not_overwritten():
    user = User.objects.create_user(username="avatar-user", email="avatar@example.test")
    import_oauth_profile(user, "User Name", "https://avatars.githubusercontent.com/example")
    user.refresh_from_db()
    assert user.full_name == "User Name" and not user.avatar_key
    import_oauth_profile(user, "Different Name", "")
    user.refresh_from_db()
    assert user.full_name == "User Name"


def test_account_deletion_queues_latest_avatar_despite_stale_request_and_broker_loss():
    from core.services import delete_account

    user = User.objects.create_user(
        username="avatar-user", email="avatar@example.test", avatar_key="old.webp"
    )
    User.objects.filter(pk=user.pk).update(avatar_key="latest.webp")
    with patch("core.tasks.delete_avatar_object.delay", side_effect=ConnectionError("broker down")):
        delete_account(user)
    assert not User.objects.filter(pk=user.pk).exists()
    assert PendingAvatarDeletion.objects.filter(key="latest.webp").exists()


def test_upload_finishing_after_account_deletion_keeps_durable_cleanup_record():
    user = User.objects.create_user(username="avatar-user", email="avatar@example.test")
    backend = Mock()

    def save(*args, **kwargs):
        User.objects.filter(pk=user.pk).delete()
        return "late-upload.webp"

    backend.save.side_effect = save
    with (
        patch("core.avatar.avatar_available", return_value=True),
        patch("core.avatar.storage", return_value=backend),
    ):
        with pytest.raises(User.DoesNotExist):
            upload_avatar(user, image_bytes())
    assert PendingAvatarDeletion.objects.filter(key="late-upload.webp").exists()


def test_deleted_object_cleanup_is_retryable_without_user_record():
    from core.tasks import delete_avatar_object, recover_avatar_deletions

    PendingAvatarDeletion.objects.create(key="orphan.webp")
    with patch("core.tasks.delete_avatar_object.delay") as enqueue:
        assert recover_avatar_deletions() == 1
        enqueue.assert_called_once_with("orphan.webp")
    backend = Mock()
    with patch("core.avatar.storage", return_value=backend):
        assert delete_avatar_object("orphan.webp") == "deleted"
    backend.delete.assert_called_once_with("orphan.webp")
    assert not PendingAvatarDeletion.objects.exists()


def test_redis_outage_does_not_break_public_avatar_fallback():
    from redis.exceptions import ConnectionError as RedisConnectionError

    configure_s3()
    backend = Mock()
    backend.connection.meta.client.head_bucket.side_effect = OSError("S3 down")
    with (
        patch("core.avatar.cache.get", side_effect=RedisConnectionError()),
        patch("core.avatar.cache.set", side_effect=RedisConnectionError()),
        patch("core.avatar.storage", return_value=backend),
    ):
        assert not avatar_available()


def test_oauth_avatar_s3_failure_does_not_cancel_name_or_registration(httpx_mock):
    from botocore.exceptions import ClientError

    user = User.objects.create_user(username="avatar-user", email="avatar@example.test")
    url = "https://avatars.githubusercontent.com/example"
    httpx_mock.add_response(url=url, content=image_bytes())
    with (
        patch("core.avatar.avatar_available", return_value=True),
        patch(
            "core.avatar.upload_avatar",
            side_effect=ClientError({"Error": {"Code": "ServiceUnavailable"}}, "PutObject"),
        ),
    ):
        import_oauth_profile(user, "Visible Name", url)
    user.refresh_from_db()
    assert user.full_name == "Visible Name" and not user.avatar_key
