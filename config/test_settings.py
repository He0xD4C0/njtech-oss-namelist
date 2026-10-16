"""Isolated test configuration; never used by WSGI/Celery deployment entrypoints."""

import os
from base64 import urlsafe_b64encode

os.environ.setdefault("DJANGO_SECRET_KEY", "isolated-test-settings-not-for-production")

from .settings import *  # noqa: F403

DEBUG = True
APP_ENCRYPTION_KEY = urlsafe_b64encode(b"0" * 32).decode()
BOOTSTRAP_SECRET = "local-test-bootstrap-secret"
SESSION_COOKIE_NAME = "sessionid"
CSRF_COOKIE_NAME = "csrftoken"
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
if os.getenv("TEST_POSTGRES", "0") != "1":
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
