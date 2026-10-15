import os
from base64 import urlsafe_b64encode

os.environ.setdefault("DJANGO_DEBUG", "1")
os.environ.setdefault("DATABASE_SQLITE", "1")
os.environ.setdefault("LOCAL_MEMORY_CACHE", "1")
os.environ.setdefault("APP_ENCRYPTION_KEY", urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("BOOTSTRAP_SECRET", "local-test-bootstrap-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.test_settings")
