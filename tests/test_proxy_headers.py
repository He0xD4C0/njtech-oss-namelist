import io
import json
import os
import runpy
from ipaddress import ip_network
from pathlib import Path
from unittest.mock import patch

import pytest
from allauth.core.internal.httpkit import get_client_ip
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from django_ratelimit.core import _get_ip
from gunicorn.config import Config
from gunicorn.http.message import Request
from gunicorn.http.unreader import IterUnreader

from core.middleware import ProxyHeadersMiddleware


@pytest.mark.parametrize("value", [None, "0", "true", "1"])
def test_forwarded_headers_require_explicit_opt_in(value):
    environment = {"DJANGO_DEBUG": "1", "TRUSTED_PROXY_CIDRS": "10.0.0.0/8"}
    with patch.dict(os.environ, environment):
        os.environ.pop("TRUST_PROXY_HEADERS", None)
        if value is not None:
            os.environ["TRUST_PROXY_HEADERS"] = value
        configuration = runpy.run_path(str(Path(__file__).resolve().parents[1] / "config/settings.py"))
    keys = (
        "TRUST_PROXY_HEADERS",
        "TRUSTED_PROXY_CIDRS",
        "SECURE_PROXY_SSL_HEADER",
        "ALLAUTH_TRUSTED_PROXY_COUNT",
        "ALLAUTH_TRUSTED_CLIENT_IP_HEADER",
        "RATELIMIT_IP_META_KEY",
    )
    with override_settings(**{key: configuration[key] for key in keys}):
        request = RequestFactory().get(
            "/",
            REMOTE_ADDR="10.126.126.5",
            HTTP_X_REAL_IP="203.0.113.9",
            HTTP_X_FORWARDED_FOR="198.51.100.1, 198.51.100.2",
            HTTP_X_FORWARDED_PROTO="https",
        )
        assert ProxyHeadersMiddleware(lambda request: HttpResponse())(request).status_code == 200
        trusted = value == "1"
        assert request.is_secure() is trusted
        expected = "203.0.113.9" if trusted else "10.126.126.5"
        assert get_client_ip(request) == expected
        assert _get_ip(request) == expected


@override_settings(
    TRUST_PROXY_HEADERS=True,
    TRUSTED_PROXY_CIDRS=(ip_network("10.0.0.0/8"),),
    ALLAUTH_TRUSTED_PROXY_COUNT=0,
    ALLAUTH_TRUSTED_CLIENT_IP_HEADER="x-real-ip",
    RATELIMIT_IP_META_KEY="HTTP_X_REAL_IP",
)
def test_xff_is_never_a_client_address_fallback():
    request = RequestFactory().get(
        "/",
        REMOTE_ADDR="10.126.126.5",
        HTTP_X_FORWARDED_FOR="198.51.100.1, 198.51.100.2",
    )
    assert ProxyHeadersMiddleware(lambda request: HttpResponse())(request).status_code == 200
    assert get_client_ip(request) == _get_ip(request) == "10.126.126.5"


@pytest.mark.parametrize("value", ["192.0.2.1, 192.0.2.2", "invalid", "", "192.0.2.1:443"])
@override_settings(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_CIDRS=(ip_network("10.0.0.0/8"),))
def test_trusted_real_ip_must_be_a_single_ip(value):
    request = RequestFactory().get("/", REMOTE_ADDR="10.126.126.5", HTTP_X_REAL_IP=value)
    assert ProxyHeadersMiddleware(lambda request: HttpResponse())(request).status_code == 400


@override_settings(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_CIDRS=(ip_network("10.0.0.0/8"),))
def test_trusted_real_ip_accepts_ipv6():
    request = RequestFactory().get("/", REMOTE_ADDR="10.126.126.5", HTTP_X_REAL_IP="2001:db8::1")
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


@pytest.mark.parametrize("networks", ["", " ", "not-a-network", "10.126.1.1/8", "10.0.0.0/33"])
def test_enabled_proxy_trust_requires_valid_explicit_networks(networks):
    with patch.dict(os.environ, {"DJANGO_DEBUG": "1", "TRUST_PROXY_HEADERS": "1", "TRUSTED_PROXY_CIDRS": networks}):
        with pytest.raises(ImproperlyConfigured, match="TRUSTED_PROXY_CIDRS"):
            runpy.run_path(str(Path(__file__).resolve().parents[1] / "config/settings.py"))


@pytest.mark.parametrize("peer,trusted", [
    ("10.126.126.5", True), ("10.5.6.7", True), ("11.0.0.1", False),
    ("127.0.0.1", False), ("172.18.0.1", False), ("", False),
])
@override_settings(
    TRUST_PROXY_HEADERS=True,
    TRUSTED_PROXY_CIDRS=(ip_network("10.0.0.0/8"),),
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
    ALLAUTH_TRUSTED_PROXY_COUNT=0,
    ALLAUTH_TRUSTED_CLIENT_IP_HEADER="x-real-ip",
    RATELIMIT_IP_META_KEY="HTTP_X_REAL_IP",
)
def test_trust_uses_socket_peer_not_claimed_client(peer, trusted):
    request = RequestFactory().get(
        "/", REMOTE_ADDR=peer, HTTP_X_REAL_IP="203.0.113.9",
        HTTP_X_FORWARDED_FOR="10.126.126.5", HTTP_X_FORWARDED_PROTO="https",
    )
    assert ProxyHeadersMiddleware(lambda request: HttpResponse())(request).status_code == 200
    assert request.is_secure() is trusted
    assert request.META["HTTP_X_REAL_IP"] == ("203.0.113.9" if trusted else peer)
    if peer:
        assert get_client_ip(request) == _get_ip(request) == ("203.0.113.9" if trusted else peer)


@pytest.mark.parametrize("protocol,expected", [("http", 200), ("https", 200), ("https,http", 400), ("ftp", 400), ("", 400)])
@override_settings(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_CIDRS=(ip_network("10.0.0.0/8"),))
def test_trusted_protocol_is_a_single_http_scheme(protocol, expected):
    request = RequestFactory().get("/", REMOTE_ADDR="10.126.126.5", HTTP_X_FORWARDED_PROTO=protocol)
    assert ProxyHeadersMiddleware(lambda request: HttpResponse())(request).status_code == expected


@override_settings(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_CIDRS=(ip_network("10.0.0.0/8"),))
def test_untrusted_malformed_headers_are_ignored():
    request = RequestFactory().get("/", REMOTE_ADDR="192.0.2.1", HTTP_X_REAL_IP="invalid", HTTP_X_FORWARDED_PROTO="invalid")
    assert ProxyHeadersMiddleware(lambda request: HttpResponse())(request).status_code == 200
    assert request.META["HTTP_X_REAL_IP"] == "192.0.2.1"
    assert "HTTP_X_FORWARDED_PROTO" not in request.META


def test_proxy_filter_precedes_all_scheme_consumers(settings):
    assert settings.MIDDLEWARE[0] == "core.middleware.ProxyHeadersMiddleware"


@pytest.mark.parametrize("peer,expected", [("10.126.126.5", 200), ("192.0.2.1", 301)])
@override_settings(
    TRUST_PROXY_HEADERS=True,
    TRUSTED_PROXY_CIDRS=(ip_network("10.0.0.0/8"),),
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
    SECURE_SSL_REDIRECT=True,
)
def test_security_middleware_sees_only_verified_scheme(peer, expected):
    from django.middleware.security import SecurityMiddleware

    request = RequestFactory().get("/", REMOTE_ADDR=peer, HTTP_X_FORWARDED_PROTO="https")
    handler = ProxyHeadersMiddleware(SecurityMiddleware(lambda request: HttpResponse()))
    assert handler(request).status_code == expected


@override_settings(
    TRUST_PROXY_HEADERS=True,
    TRUSTED_PROXY_CIDRS=(ip_network("2001:db8::/32"),),
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
)
def test_trusted_ipv6_peer_without_headers_uses_connection():
    request = RequestFactory().get("/", REMOTE_ADDR="2001:db8::1")
    assert ProxyHeadersMiddleware(lambda request: HttpResponse())(request).status_code == 200
    assert not request.is_secure()
    assert request.META["HTTP_X_REAL_IP"] == "2001:db8::1"
