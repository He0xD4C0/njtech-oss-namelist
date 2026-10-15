"""Check Django initialization and live DB/cache connectivity without a public health endpoint."""

import os
import socket
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()
from django.core.cache import cache  # noqa: E402 - app registry must be initialized first
from django.db import connection  # noqa: E402

try:
    with socket.create_connection(("127.0.0.1", 8000), timeout=2):
        pass
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    cache.set("healthcheck:web", 1, timeout=60)
    if cache.get("healthcheck:web") != 1:
        raise RuntimeError("cache unavailable")
except Exception:
    # Do not log connection strings or exception text containing credentials.
    print("database or cache unavailable", file=sys.stderr)
    sys.exit(1)
