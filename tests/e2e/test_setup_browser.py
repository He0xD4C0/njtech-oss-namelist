import re
from unittest.mock import patch

import pytest
from allauth.account.models import EmailAddress
from django.core import mail
from playwright.sync_api import expect

from core.models import PolicyDocument, SiteState, SyncRun, User
from core.runtime import get_setting

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.e2e]


def no_overflow(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), page.locator("body *").evaluate_all(
        "nodes => nodes.filter(n => n.getBoundingClientRect().right > innerWidth).map(n => [n.tagName, n.className]).slice(-10)"
    )


def test_six_step_wizard_with_cross_device_confirmation(page, browser, live_server, settings, artifact_dir):
    page.goto(live_server.url + "/not-a-route")
    expect(page).to_have_url(live_server.url + "/setup/")
    page.get_by_label("服务器初始化密钥").fill(settings.BOOTSTRAP_SECRET)
    page.get_by_role("button", name="验证并开始").click()
    smtp = page.locator("#smtp")
    smtp.get_by_label("SMTP host", exact=True).fill("smtp.example.test")
    smtp.get_by_label("SMTP port", exact=True).fill("not-a-port")
    smtp.get_by_role("button", name="保存此组配置").click()
    expect(smtp.get_by_role("alert")).to_contain_text("Invalid TCP port")
    expect(smtp.locator("#id_smtp_port")).to_have_attribute("aria-invalid", "true")
    smtp.get_by_label("SMTP port", exact=True).fill("587")
    smtp.get_by_role("button", name="保存此组配置").click()
    page.get_by_label("用户名 / Username", exact=False).fill("initial-admin")
    page.get_by_label("管理员邮箱 / Administrator email", exact=False).fill("initial@example.test")
    page.get_by_label("密码 / Password", exact=False).fill("Initial-passphrase-browser-872!")
    page.get_by_role("button", name="创建并发送验证邮件").click()
    expect(page.get_by_text("等待邮箱验证：", exact=False)).to_be_visible()
    page.get_by_role("button", name="重新发送验证邮件").click()
    path = re.search(r"/accounts/confirm-email/[^\s]+/", mail.outbox[-1].body).group(0)
    device = browser.new_context()
    confirmation = device.new_page()
    confirmation.goto(live_server.url + path)
    confirmation.get_by_role("button", name="确认此邮箱").click()
    expect(confirmation.get_by_label("服务器初始化密钥")).to_be_visible()
    expect(confirmation.get_by_text("邮箱已确认。", exact=False)).to_be_visible()
    device.close()
    page.get_by_role("link", name="刷新状态", exact=True).click()
    expect(page.get_by_text("邮箱已验证。", exact=True)).to_be_visible()
    page.get_by_role("link", name="3 · 安全与策略").click()
    page.get_by_label("Turnstile site key").fill("test-only-site-key")
    page.get_by_label("Turnstile secret key").fill("test-only-secret")
    page.locator("#turnstile").get_by_role("button", name="保存此组配置").click()
    page.get_by_label("Policy documents repository URL").fill("https://github.com/example/policies")
    page.locator("#documents").get_by_role("button", name="保存此组配置").click()

    # External Git acquisition is replaced, while UI, DB writes and completion display are real.
    def sync():
        for kind in ("privacy", "terms", "inclusion"):
            PolicyDocument.objects.create(kind=kind, language="zh-CN", body="# Test policy", source_sha="b" * 40)
        SyncRun.objects.create(kind="documents", status="success", message="Test source synchronized")

    with patch("core.tasks.sync_documents.delay", side_effect=sync) as task:
        page.locator("#documents").get_by_role("button", name="同步已保存的来源").click()
        expect(page.get_by_text("Test source synchronized")).to_be_visible()
        page.get_by_role("link", name="刷新状态", exact=True).click()
        task.assert_called_once()
    page.get_by_role("link", name="4 · 平台与存储").click()
    page.get_by_role("link", name="继续 / 跳过未配置项", exact=False).click()
    expect(page.get_by_text("尚未收到维护服务的有效状态", exact=False)).to_be_visible()
    page.get_by_label("恢复演练频率 / Restore drill frequency").select_option("off")
    page.get_by_role("button", name="保存此组配置").click()
    page.get_by_role("link", name="继续 / 跳过未配置项", exact=False).click()
    for width in (390, 768, 1440):
        page.set_viewport_size({"width": width, "height": 900})
        for mode in ("light", "dark", "auto"):
            page.get_by_label("配色模式").select_option(mode)
            no_overflow(page)
        page.screenshot(path=str(artifact_dir / f"setup-review-{width}.png"), full_page=True)
    expect(page.get_by_role("button", name="完成激活并开放站点")).to_be_enabled()
    page.get_by_role("button", name="完成激活并开放站点").click()
    expect(page).to_have_url(live_server.url + "/accounts/login/")
    assert SiteState.objects.get().activated_at
    assert get_setting("smtp_host") == "smtp.example.test"
    assert get_setting("turnstile_secret_key") == "test-only-secret"
    assert get_setting("restore_schedule") == "off"
    assert page.request.get(live_server.url + "/setup/").status == 404


def test_shared_theme_responsive_workspace_and_admin(page, live_server, initialized_site, session_login, artifact_dir):
    failures = []
    page.on("pageerror", lambda error: failures.append(str(error)))
    user = User.objects.create_superuser("theme-admin", "theme@example.test", "Browser-theme-password-41!")
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    session_login(user)
    page.emulate_media(color_scheme="dark")
    page.goto(live_server.url + "/")
    expect(page.locator("html")).to_have_attribute("data-bs-theme", "dark")
    page.emulate_media(color_scheme="light")
    expect(page.locator("html")).to_have_attribute("data-bs-theme", "light")
    page.get_by_label("配色模式").select_option("dark")
    page.goto(live_server.url + "/manage/settings/")
    expect(page.locator("html")).to_have_class(re.compile(r"dark"))
    expect(page.get_by_label("配色模式")).to_have_value("dark")
    page.get_by_label("配色模式").select_option("light")
    page.goto(live_server.url + "/account/profile/")
    expect(page.locator("html")).to_have_attribute("data-bs-theme", "light")
    for width in (390, 768, 1440):
        page.set_viewport_size({"width": width, "height": 900})
        for path in ("/", "/repositories/", "/rankings/", "/announcements/", "/source/", "/u/theme-admin/", "/account/profile/", "/platforms/", "/submit/", "/accounts/email/", "/accounts/password/change/", "/account/delete/", "/manage/settings/", "/admin/django_celery_beat/periodictask/add/", "/admin/sites/site/", "/admin/bulletins/announcement/add/"):
            page.goto(live_server.url + path)
            for mode in ("light", "dark", "auto"):
                page.get_by_label("配色模式").select_option(mode)
                expect(page.locator("html")).to_have_attribute("data-bs-theme", "dark" if mode == "dark" else "light")
                no_overflow(page)
            if path == "/account/profile/":
                if width < 992:
                    page.get_by_role("button", name="菜单", exact=True).click()
                    expect(page.locator("#account-navigation")).to_have_class(re.compile(r"\bshow\b"))
                expect(page.get_by_role("link", name="平台绑定", exact=True)).to_be_visible()
                page.screenshot(path=str(artifact_dir / f"workspace-{width}.png"), full_page=True)
            if path == "/admin/bulletins/announcement/add/":
                if width < 1280:
                    toggle = page.get_by_role("button", name="展开管理导航").first
                    toggle.focus()
                    page.keyboard.press("Enter")
                    expect(page.locator("#nav-sidebar")).to_be_visible()
                    toggle.click()
                page.get_by_label("配色模式").select_option("dark")
                page.emulate_media(color_scheme="dark")
                expect(page.locator(".CodeMirror")).to_be_visible()
                assert page.locator(".CodeMirror").evaluate("n => getComputedStyle(n).backgroundColor") == "rgb(24, 36, 51)"
                page.screenshot(path=str(artifact_dir / f"editor-dark-{width}.png"), full_page=True)
                page.get_by_label("配色模式").select_option("auto")
                page.emulate_media(color_scheme="light")
                expect(page.locator("html")).not_to_have_class(re.compile(r"dark"))
                page.emulate_media(color_scheme="dark")
                expect(page.locator("html")).to_have_class(re.compile(r"dark"))
                page.emulate_media(color_scheme="light")
    assert not failures, failures


def test_anonymous_forms_language_and_keyboard_navigation(page, live_server, external_services_test_only):
    for width in (390, 768, 1440):
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(live_server.url + "/")
        if width == 390:
            toggle = page.get_by_role("button", name="展开主导航")
            toggle.focus()
            page.keyboard.press("Enter")
            expect(page.locator("#site-navigation")).to_have_class(re.compile(r"\bshow\b"))
            expect(page.get_by_role("link", name="月榜", exact=True)).to_be_visible()
        page.get_by_label("界面语言", exact=True).select_option("en")
        page.get_by_role("button", name="切换语言").click()
        expect(page.locator("html")).to_have_attribute("lang", "en")
        for path in ("/accounts/login/", "/accounts/signup/", "/accounts/password/reset/", "/policies/terms/?language=en-US"):
            page.goto(live_server.url + path)
            for mode in ("light", "dark", "auto"):
                page.get_by_label("Color mode").select_option(mode)
                no_overflow(page)
        page.get_by_label("Interface language", exact=True).select_option("zh-hans")
        page.get_by_role("button", name="Apply language").click()
        expect(page.locator("html")).to_have_attribute("lang", "zh-hans")
