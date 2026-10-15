import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import sync_playwright


@pytest.fixture
def browser(monkeypatch):
    # Playwright's sync API keeps an event loop active via greenlets. Test ORM
    # operations remain sequential; this exception is scoped to this fixture only.
    monkeypatch.setenv("DJANGO_ALLOW_ASYNC_UNSAFE", "true")
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if not executable:
        cache = Path.home() / "Library/Caches/ms-playwright"
        candidates = sorted(cache.glob("chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium"))
        if candidates:
            executable = str(candidates[-1])
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True, executable_path=executable or None)
        yield instance
        instance.close()


@pytest.fixture
def page(browser, live_server):
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    origin = urlsplit(live_server.url).netloc

    # No real OAuth, challenge, image storage or other external services in local acceptance tests.
    def restrict(route):
        if urlsplit(route.request.url).netloc == origin:
            route.continue_()
        else:
            route.abort()

    context.route("**/*", restrict)
    tab = context.new_page()
    yield tab
    context.close()


@pytest.fixture
def session_login(page, live_server):
    from django.test import Client

    def login(user):
        client = Client()
        client.force_login(user)
        page.context.add_cookies(
            [
                {
                    "name": "sessionid",
                    "value": client.cookies["sessionid"].value,
                    "url": live_server.url,
                    "httpOnly": True,
                    "sameSite": "Lax",
                }
            ]
        )

    return login


@pytest.fixture
def artifact_dir(tmp_path):
    directory = Path(os.getenv("NAMELIST_E2E_ARTIFACT_DIR", str(tmp_path)))
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture
def external_services_test_only(monkeypatch):
    from django.utils import timezone

    from core.models import PolicyDocument, SiteState
    from core.runtime import set_setting

    monkeypatch.setattr("core.forms.verify_turnstile", lambda request: None)
    SiteState.objects.create(pk=1, activated_at=timezone.now())
    for key, value in (
        ("smtp_host", "local.invalid"),
        ("smtp_verified", "1"),
        ("turnstile_site_key", "test-only-sitekey"),
        ("turnstile_secret_key", "test-only-secret"),
    ):
        set_setting(key, value)
    for language in ("zh-CN", "en-US"):
        for kind in ("privacy", "terms", "inclusion"):
            PolicyDocument.objects.create(
                kind=kind, language=language, body=f"# {kind}\nTest policy.", source_sha="a" * 40
            )
