import io
import uuid

import httpx
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import transaction
from PIL import Image, ImageOps, UnidentifiedImageError
from redis.exceptions import RedisError
from storages.backends.s3 import S3Storage

from .models import PendingAvatarDeletion, User
from .runtime import get_setting

MAX_BYTES = 5 * 1024 * 1024


def storage():
    if not all(get_setting(key) for key in ("s3_bucket", "s3_access_key", "s3_secret_key")):
        raise ValueError("Avatar storage is not configured")
    return S3Storage(
        bucket_name=get_setting("s3_bucket"),
        endpoint_url=get_setting("s3_endpoint") or None,
        region_name=get_setting("s3_region") or None,
        access_key=get_setting("s3_access_key"),
        secret_key=get_setting("s3_secret_key"),
        location=get_setting("s3_prefix", "avatars"),
        default_acl=None,
        file_overwrite=False,
        querystring_auth=True,
        querystring_expire=300,
        client_config=Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 1}),
    )


def avatar_available():
    if (
        not all(get_setting(key) for key in ("s3_bucket", "s3_access_key", "s3_secret_key"))
        or get_setting("s3_verified") != "1"
    ):
        return False
    try:
        cached = cache.get("avatar-storage-health")
    except (RedisError, OSError):
        cached = None
    if cached is not None:
        return cached
    try:
        storage().connection.meta.client.head_bucket(Bucket=get_setting("s3_bucket"))
        healthy = True
    except Exception:
        healthy = False
    try:
        cache.set("avatar-storage-health", healthy, 30)
    except (RedisError, OSError):
        pass
    return healthy


def avatar_url(user):
    if getattr(user, "avatar_key", "") and avatar_available():
        try:
            return storage().url(user.avatar_key)
        except Exception:
            pass
    return "/static/img/default-avatar.svg"


def normalized_avatar(data):
    if len(data) > MAX_BYTES:
        raise ValueError("头像不能超过 5 MiB / Avatar must not exceed 5 MiB")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 20_000_000:
                raise ValueError("头像像素过大 / Avatar dimensions are too large")
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((512, 512))
            out = io.BytesIO()
            image.save(out, format="WEBP", quality=85)
            return out.getvalue()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise ValueError("无效的头像图片 / Invalid avatar image") from exc


def queue_avatar_deletion(key):
    if not key:
        return
    PendingAvatarDeletion.objects.get_or_create(key=key)

    def dispatch():
        from .tasks import delete_avatar_object

        try:
            delete_avatar_object.delay(key)
        except Exception:
            pass  # Hourly cleanup recovers the durable record after broker failure.

    transaction.on_commit(dispatch)


def upload_avatar(user, data, *, import_revision=None):
    if not avatar_available():
        raise ValueError("头像存储暂不可用 / Avatar storage is unavailable")
    data = normalized_avatar(data)
    backend = storage()
    key = backend.save(f"{user.pk}/{uuid.uuid4().hex}.webp", ContentFile(data))
    old_key = ""
    try:
        with transaction.atomic():
            current = User.objects.select_for_update().get(pk=user.pk)
            if import_revision is not None and (
                current.avatar_key or current.avatar_revision != import_revision
            ):
                queue_avatar_deletion(key)
                return False
            old_key = current.avatar_key
            current.avatar_key = key
            current.avatar_revision += 1
            current.save(update_fields=["avatar_key", "avatar_revision"])
            queue_avatar_deletion(old_key)
            user.avatar_key, user.avatar_revision = current.avatar_key, current.avatar_revision
    except Exception:
        queue_avatar_deletion(key)
        raise
    return True


def import_avatar(user, url):
    if user.avatar_key or not url or not avatar_available():
        return
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    allowed = (
        "avatars.githubusercontent.com",
        "github.com",
        "gitee.com",
        "gitlab.com",
        "secure.gravatar.com",
        "www.gravatar.com",
    )
    if parsed.scheme != "https" or parsed.hostname not in allowed or parsed.username:
        return
    revision = user.avatar_revision
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        return
        upload_avatar(user, bytes(data), import_revision=revision)
    except (httpx.HTTPError, ValueError, BotoCoreError, ClientError, OSError):
        return


def test_storage():
    backend = storage()
    key = backend.save(f"health/{uuid.uuid4().hex}.txt", ContentFile(b"namelist-health-check"))
    try:
        with backend.open(key, "rb") as handle:
            if handle.read() != b"namelist-health-check":
                raise ValueError("S3 readback did not match")
    finally:
        backend.delete(key)
