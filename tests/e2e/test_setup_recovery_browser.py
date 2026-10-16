import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from allauth.account.models import EmailAddress
from django.utils import timezone
from playwright.sync_api import expect

from core.models import PolicyDocument, SiteState, SyncRun, User
from core.runtime import set_setting

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.e2e]


@pytest.fixture
def recovery_page(page, live_server, settings):
    user = User.objects.create_superuser("admin", email="recovery@example.test")
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    SiteState.objects.create(pk=1, bootstrap_user=user)
    for key, value in {"smtp_host": "smtp.example.test", "smtp_verified": "1", "turnstile_site_key": "site",
                       "turnstile_secret_key": "secret", "license_source_url": "https://github.com/example/rules.git"}.items():
        set_setting(key, value)
    for kind in ("privacy", "terms", "inclusion"):
        PolicyDocument.objects.create(kind=kind, language="zh-CN", body="policy", source_sha="a")
    run = SyncRun.objects.create(kind="licenses", status="queued")
    # Deterministic delayed read responses, including responses too late to cancel.
    # Production code has no timing/test bypasses.
    page.add_init_script("""(() => {
      const interval = window.setInterval;
      window.setInterval = (fn, ms, ...args) => ms === 5000 ? (window.testPoll = fn, 0) : interval(fn, ms, ...args);
      const fetch = window.fetch;
      window.delayedReads = [];
      window.fetch = async (url, options = {}) => {
        const isStatus = String(url).includes('action=status');
        const isFragment = String(url).includes('task=licenses') && options.headers?.Accept === 'application/json';
        if ((window.holdRead === 'status' && isStatus) || (window.holdRead === 'fragment' && isFragment)) {
          window.holdRead = null;
          const response = await fetch(url, {...options, signal: undefined});
          return new Promise(resolve => window.delayedReads.push({signal: options.signal,
            release: replacement => resolve(replacement ? new Response(JSON.stringify(replacement)) : response)}));
        }
        return fetch(url, options);
      };
    })();""")
    page.goto(live_server.url + "/setup/")
    page.get_by_label("服务器初始化密钥").fill(settings.BOOTSTRAP_SECRET)
    page.get_by_role("button", name="验证并开始").click()
    expect(page.get_by_role("heading", name="创建初始管理员", exact=True)).to_be_visible()
    page.goto(live_server.url + "/setup/?step=requirements&task=licenses")
    return page, run


@pytest.mark.parametrize("phase,action", [("status", "navigate"), ("fragment", "navigate"),
                                           ("fragment", "return"), ("fragment", "submit"),
                                           ("fragment", "input"), ("fragment", "redirect"),
                                           ("status", "unauthorized"), ("fragment", "hidden")])
def test_late_poll_cannot_overwrite_current_operation(recovery_page, phase, action):
    page, run = recovery_page
    run.status, run.execution_token = "running", uuid.uuid4()
    run.lease_expires_at = timezone.now() + timedelta(seconds=120)
    run.save()
    page.evaluate("phase => { window.holdRead = phase; window.testPoll(); }", phase)
    page.wait_for_function("window.delayedReads.length === 1")
    if action == "input":
        page.get_by_label("许可证策略仓库").fill("https://github.com/example/unsaved.git")
    elif action == "hidden":
        page.evaluate("Object.defineProperty(document, 'hidden', {value: true, configurable: true}); document.dispatchEvent(new Event('visibilitychange'))")
    elif action == "submit":
        page.get_by_role("button", name="稍后配置", exact=True).click()
        expect(page.get_by_role("heading", name="绑定 GitHub", exact=True)).to_be_visible()
    else:
        page.get_by_role("link", name="上一步", exact=True).click()
        expect(page.get_by_role("heading", name="同步政策正文", exact=True)).to_be_visible()
        if action == "return":
            page.get_by_role("link", name="继续", exact=True).click()
            expect(page.get_by_role("heading", name="配置许可证策略", exact=True)).to_be_visible()
    url = page.url
    page.evaluate("document.querySelector('#setup-flow').dataset.testMarker = 'current'")
    assert page.evaluate("window.delayedReads[0].signal.aborted")
    replacement = {"redirect": "/accounts/login/"} if action == "redirect" else {"authorized": False} if action == "unauthorized" else None
    page.evaluate("replacement => window.delayedReads[0].release(replacement)", replacement)
    page.wait_for_timeout(150)
    assert page.url == url
    expect(page.locator("#setup-flow")).to_have_attribute("data-test-marker", "current")
    expect(page.locator("#setup-connection-error")).not_to_be_visible()
    if action == "input":
        expect(page.get_by_label("许可证策略仓库")).to_have_value("https://github.com/example/unsaved.git")


def test_poll_updates_recovery_button_and_manual_retry_once(recovery_page, artifact_dir):
    page, run = recovery_page
    run.status, run.execution_token = "running", uuid.uuid4()
    run.lease_expires_at = timezone.now() + timedelta(seconds=40)
    run.save()
    page.evaluate("window.testPoll()")
    expect(page.get_by_text("执行者暂未续期", exact=False)).to_be_visible()
    expect(page.get_by_role("button", name="重新尝试", exact=True)).not_to_be_visible()
    SyncRun.objects.filter(pk=run.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    page.evaluate("window.testPoll()")
    expect(page.get_by_role("button", name="重新尝试", exact=True)).to_be_visible()
    for width in (390, 1440):
        page.set_viewport_size({"width": width, "height": 900})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(artifact_dir / f"sync-recovery-{width}.png"), full_page=True)
    with patch("core.tasks.sync_license_policy.delay") as dispatch:
        page.get_by_role("button", name="重新尝试", exact=True).click()
        expect(page.get_by_text("请求已提交，正在等待执行", exact=False)).to_be_visible()
        dispatch.assert_called_once()
        assert dispatch.call_args.kwargs["run_id"] == run.pk
    run.refresh_from_db()
    assert run.status == "queued"
