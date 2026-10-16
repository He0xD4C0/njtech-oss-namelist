import os
from base64 import urlsafe_b64encode

import pytest

os.environ.setdefault("DJANGO_DEBUG", "1")
os.environ.setdefault("DATABASE_SQLITE", "1")
os.environ.setdefault("LOCAL_MEMORY_CACHE", "1")
os.environ.setdefault("APP_ENCRYPTION_KEY", urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("BOOTSTRAP_SECRET", "local-test-bootstrap-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.test_settings")


@pytest.fixture(autouse=True)
def isolated_cache():
    # Rate limits and readiness reports must not leak between independent tests.
    # Limits remain enabled and are exercised normally within each test.
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def initialized_site(db):
    """Business tests run against an explicitly activated installation."""
    from django.utils import timezone

    from core.models import SiteState

    return SiteState.objects.update_or_create(pk=1, defaults={"activated_at": timezone.now()})[0]
