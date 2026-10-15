from cryptography.fernet import Fernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def cipher():
    if not settings.APP_ENCRYPTION_KEY:
        raise ImproperlyConfigured("APP_ENCRYPTION_KEY must be provided through server Secrets")
    try:
        return Fernet(settings.APP_ENCRYPTION_KEY.encode())
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured("APP_ENCRYPTION_KEY is not a valid Fernet key") from exc


def encrypt(value):
    return cipher().encrypt(value.encode()).decode() if value else ""


def decrypt(value):
    return cipher().decrypt(value.encode()).decode() if value else ""
