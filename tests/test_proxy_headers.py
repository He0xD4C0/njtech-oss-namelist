import io
import json
import os
import runpy
from pathlib import Path
from unittest.mock import patch

import pytest
from allauth.core.internal.httpkit import get_client_ip
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from django_ratelimit.core import _get_ip
from gunicorn.config import Config
from gunicorn.http.message import Request
from gunicorn.http.unreader import IterUnreader

from core.middleware import ProxyHeadersMiddleware


@pytest.mark.parametrize("value", [None, "0", "true", "1"])
def test_forwarded_headers_require_explicit_opt_in(value):
    environment = {"DJANGO_DEBUG": "1"}
    with patch.dict(os.environ, environment):
        os.environ.pop("TRUST_PROXY_HEADERS", None)
        if value is not None:
            os.environ["TRUST_PROXY_HEADERS"] = value
        configuration = runpy.run_path(str(Path(__file__).resolve().parents[1] / "config/settings.py"))
    keys = (
        "TRUST_PROXY_HEADERS",
        "SECURE_PROXY_SSL_HEADER",
        "ALLAUTH_TRUSTED_PROXY_COUNT",
        "ALLAUTH_TRUSTED_CLIENT_IP_HEADER",
        "RATELIMIT_IP_META_KEY",
    )
    with override_settings(**{key: configuration[key] for key in keys}):
        request = RequestFactory().get(
            "/",
            REMOTE_ADDR="192.0.2.1",
            HTTP_X_REAL_IP="203.0.113.9",
            HTTP_X_FORWARDED_FOR="198.51.100.1, 198.51.100.2",
            HTTP_X_FORWARDED_PROTO="https",
        )
        assert ProxyHeadersMiddleware(lambda request: HttpResponse())(request).status_code == 200
        trusted = value == "1"
        assert request.is_secure() is trusted
        expected = "203.0.113.9" if trusted else "192.0.2.1"
        assert get_client_ip(request) == expected
        assert _get_ip(request) == expected


@override_settings(
    TRUST_PROXY_HEADERS=True,
    ALLAUTH_TRUSTED_PROXY_COUNT=0,
    ALLAUTH_TRUSTED_CLIENT_IP_HEADER="x-real-ip",
    RATELIMIT_IP_META_KEY="HTTP_X_REAL_IP",
)
def test_xff_is_never_a_client_address_fallback():
    request = RequestFactory().get(
        "/",
        REMOTE_ADDR="192.0.2.1",
        HTTP_X_FORWARDED_FOR="198.51.100.1, 198.51.100.2",
    )
    assert ProxyHeadersMiddleware(lambda request: HttpResponse())(request).status_code == 200
    assert get_client_ip(request) == _get_ip(request) == "192.0.2.1"


@pytest.mark.parametrize("value", ["192.0.2.1, 192.0.2.2", "invalid", "", "192.0.2.1:443"])
@override_settings(TRUST_PROXY_HEADERS=True)
def test_trusted_real_ip_must_be_a_single_ip(value):
    request = RequestFactory().get("/", HTTP_X_REAL_IP=value)
    assert ProxyHeadersMiddleware(lambda request: HttpResponse())(request).status_code == 400


@override_settings(TRUST_PROXY_HEADERS=True)
def test_trusted_real_ip_accepts_ipv6():
    request = RequestFactory().get("/", HTTP_X_REAL_IP="2001:db8::1")
    assert ProxyHeadersMiddleware(lambda request: HttpResponse())(request).status_code == 200
    assert request.META["HTTP_X_REAL_IP"] == "2001:db8::1"


def test_gunicorn_does_not_override_application_header_policy():
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    command = next(
        json.loads(line.removeprefix("CMD "))
        for line in dockerfile.splitlines()
        if line.startswith('CMD ["gunicorn"')
    )
    config = Config()
    index = command.index("--forwarded-allow-ips")
    config.set("forwarded_allow_ips", command[index + 1])
    raw = b"GET / HTTP/1.1\r\nHost: localhost\r\nX-Forwarded-Proto: https\r\nX-Forwarded-SSL: on\r\n\r\n"
    request = Request(config, IterUnreader(io.BytesIO(raw)), ("127.0.0.1", 12345))
    assert request.scheme == "http"
