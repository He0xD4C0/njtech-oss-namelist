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
    expect(page.get_by_role("heading", name="连接邮件服务", exact=True)).to_be_visible()
    page.get_by_label("邮件服务器地址", exact=True).fill("https://smtp.example.test")
    page.get_by_label("发件邮箱", exact=True).fill("sender@example.test")
    expect(page.get_by_label("测试收件邮箱")).to_have_value("sender@example.test")
    page.get_by_role("button", name="保存并发送测试邮件").click()
    expect(page.get_by_text("只填写服务器主机名", exact=False)).to_be_visible()
    expect(page.locator("#id_smtp_host")).to_have_attribute("aria-invalid", "true")
    page.get_by_label("邮件服务器地址", exact=True).fill("smtp.example.test")
    page.get_by_label("端口", exact=True).fill("994")
    page.get_by_label("直接加密（SSL/TLS）", exact=True).check()
    expect(page.get_by_label("端口", exact=True)).to_have_value("994")
    page.get_by_role("button", name="保存并发送测试邮件").click()
    expect(page.get_by_role("heading", name="创建初始管理员", exact=True)).to_be_visible()
    assert not User.objects.exists()
    assert mail.outbox[-1].to == ["sender@example.test"]
    expect(page.get_by_label("用户名", exact=True)).to_have_value("admin")
    expect(page.get_by_label("用户名", exact=True)).to_be_disabled()
    page.get_by_label("管理员邮箱", exact=True).fill("initial@example.test")
    page.get_by_label("登录密码", exact=True).fill("Initial-passphrase-browser-872!")
    page.get_by_role("button", name="发送验证邮件", exact=True).click()
    expect(page.locator('[data-mailbox-status]')).to_contain_text("验证邮件已发送")
    assert not User.objects.exists()
    path = re.search(r"/accounts/confirm-email/[^\s]+/", mail.outbox[-1].body).group(0)
    device = browser.new_context()
    confirmation = device.new_page()
    confirmation.goto(live_server.url + path)
    confirmation.get_by_role("button", name="确认此邮箱").click()
    expect(confirmation.get_by_text("邮箱已确认。", exact=False)).to_be_visible()
    device.close()
    expect(page.locator('[data-mailbox-status]')).to_contain_text("邮箱已验证", timeout=12000)
    assert not User.objects.exists()
    page.get_by_role("button", name="创建超管", exact=True).click()
    expect(page.get_by_role('heading', name='配置安全验证', exact=True)).to_be_visible()
    page.get_by_label("站点公钥", exact=False).fill("test-only-site-key")
    page.get_by_label("验证密钥", exact=False).fill("test-only-secret")
    page.get_by_role("button", name="保存并继续", exact=True).click()
    page.get_by_label("政策正文仓库").fill("https://github.com/example/policies")

    def sync(**kwargs):
        for kind in ("privacy", "terms", "inclusion"):
            PolicyDocument.objects.create(kind=kind, language="zh-CN", body="# Test policy", source_sha="b" * 40)
        SyncRun.objects.filter(pk=kwargs["run_id"]).update(status="success", source_sha="b" * 40)

    with patch("core.tasks.sync_documents.delay", side_effect=sync) as task:
        page.get_by_role("button", name="保存并同步", exact=True).click()
        expect(page.get_by_text("本次同步已完成", exact=True)).to_be_visible()
        page.reload()
        task.assert_called_once()
    page.get_by_role("link", name="继续", exact=True).click()
    for title in ("配置许可证策略", "绑定 Github", "绑定 Gitee", "绑定 Gitlab", "配置头像存储", "站点 Logo", "数据库备份"):
        # One optional task at a time; prior configuration is preserved.
        expect(page.locator('#setup-task-title')).to_contain_text(title, ignore_case=True)
        page.get_by_role("button", name="稍后配置", exact=True).click()
    expect(page.get_by_text("尚未收到维护服务的有效状态", exact=False)).to_be_visible()
    page.get_by_label("恢复演练频率", exact=True).select_option("off")
    page.get_by_role("button", name="保存并继续", exact=True).click()
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
    user = User.objects.create_superuser('admin', "theme@example.test", "Browser-theme-password-41!")
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
        for path in ("/", "/repositories/", "/rankings/", "/announcements/", "/source/", "/u/admin/", "/account/profile/", "/platforms/", "/submit/", "/accounts/email/", "/accounts/password/change/", "/account/delete/", "/manage/settings/", "/admin/django_celery_beat/periodictask/add/", "/admin/sites/site/", "/admin/bulletins/announcement/add/"):
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
        expect(page.locator("html")).to_have_attribute("lang", "en")
        expect(page.get_by_role("button", name="Apply language")).to_have_count(0)
        for path in ("/accounts/login/", "/accounts/signup/", "/accounts/password/reset/", "/policies/terms/?language=en-US"):
            page.goto(live_server.url + path)
            for mode in ("light", "dark", "auto"):
                page.get_by_label("Color mode").select_option(mode)
                no_overflow(page)
        page.get_by_label("Interface language", exact=True).select_option("zh-hans")
        expect(page.locator("html")).to_have_attribute("lang", "zh-hans")
        expect(page).to_have_url(live_server.url + "/policies/terms/?language=en-US")


def test_busy_failure_and_network_recovery(page, live_server, settings):
    import time
    from smtplib import SMTPAuthenticationError

    page.goto(live_server.url + '/setup/')
    page.get_by_label('服务器初始化密钥').fill(settings.BOOTSTRAP_SECRET)
    page.get_by_role('button', name='验证并开始').click()
    page.get_by_label('邮件服务器地址', exact=True).fill('smtp.example.test')
    page.get_by_label('发件邮箱', exact=True).fill('sender@example.test')

    def slow_failure(recipient):
        time.sleep(0.8)
        raise SMTPAuthenticationError(535, b'private-smtp-reply')

    with patch('core.views.send_smtp_test', side_effect=slow_failure) as send:
        page.get_by_role('button', name='保存并发送测试邮件').click()
        expect(page.get_by_role('button', name='正在处理，请稍候…')).to_be_disabled()
        expect(page.get_by_text('邮箱认证失败', exact=False)).to_be_visible()
        send.assert_called_once()
    expect(page.get_by_label('邮件服务器地址', exact=True)).to_have_value('smtp.example.test')
    assert 'private-smtp-reply' not in page.locator('#setup-flow').inner_text()

    def lose_response(route):
        if route.request.method == 'POST':
            route.fetch()  # The server completes, but the browser loses the response.
            route.abort()
        else:
            route.continue_()

    page.route('**/setup/', lose_response)
    with patch('core.views.send_smtp_test', return_value=1) as send:
        page.get_by_role('button', name='保存并发送测试邮件').click()
        expect(page.locator('#setup-connection-error')).to_be_visible()
        expect(page.get_by_role('button', name='保存并发送测试邮件')).to_be_disabled()
        assert get_setting('smtp_verified') == '1'
        page.get_by_role('link', name='检查当前状态', exact=True).click()
        expect(page.get_by_role('button', name='保存并发送测试邮件')).to_be_enabled()
        send.assert_called_once()
    page.unroute('**/setup/', lose_response)
    assert not User.objects.exists()


def test_no_javascript_setup_still_works(browser, live_server, settings):
    context = browser.new_context(java_script_enabled=False)
    page = context.new_page()
    page.goto(live_server.url + '/setup/')
    page.get_by_label('服务器初始化密钥').fill(settings.BOOTSTRAP_SECRET)
    page.get_by_role('button', name='验证并开始').click()
    page.get_by_label('邮件服务器地址', exact=True).fill('smtp.example.test')
    page.get_by_label('发件邮箱', exact=True).fill('sender@example.test')
    page.get_by_role('button', name='保存并发送测试邮件').click()
    expect(page.get_by_role('heading', name='创建初始管理员', exact=True)).to_be_visible()
    assert mail.outbox[-1].to == ['sender@example.test']
    context.close()


def test_focused_task_responsive_themes_and_saved_secrets(page, live_server, settings, artifact_dir):
    from core.runtime import set_setting

    user = User.objects.create_superuser('admin', email='preview@example.test')
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    SiteState.objects.update_or_create(pk=1, defaults={'bootstrap_user': user})
    for key, value in {'smtp_host': 'smtp.example.test', 'smtp_port': '994', 'smtp_security': 'tls',
                       'smtp_from_email': 'preview@example.test', 'smtp_password': 'never-render-this',
                       'smtp_verified': '1', 'turnstile_site_key': 'test-site', 'turnstile_secret_key': 'test-secret'}.items():
        set_setting(key, value)
    for kind in ('privacy', 'terms', 'inclusion'):
        PolicyDocument.objects.create(kind=kind, language='zh-CN', body='Policy', source_sha='a' * 40)
    page.goto(live_server.url + '/setup/')
    page.get_by_label('服务器初始化密钥').fill(settings.BOOTSTRAP_SECRET)
    page.get_by_role('button', name='验证并开始').click()
    expect(page.get_by_role('heading', name='创建初始管理员', exact=True)).to_be_visible()
    for task, step in [('smtp', 'mail'), ('documents', 'requirements'), ('storage', 'integrations'), ('operations', 'operations')]:
        page.goto(live_server.url + f'/setup/?step={step}&task={task}')
        for width in (390, 768, 1440):
            page.set_viewport_size({'width': width, 'height': 900})
            for mode in ('light', 'dark', 'auto'):
                page.get_by_label('配色模式').select_option(mode)
                no_overflow(page)
                expect(page.locator('#setup-task-title')).to_be_visible()
                if task == 'smtp':
                    expect(page.get_by_label('密码或客户端授权码')).to_have_value('')
                if mode in ('light', 'dark') and width in (390, 1440):
                    page.screenshot(path=str(artifact_dir / f'focused-{task}-{mode}-{width}.png'), full_page=True)
    page.goto(live_server.url + '/setup/?step=integrations&task=storage')
    page.get_by_label('存储服务地址', exact=False).fill('https://account.r2.cloudflarestorage.com/bucket')
    page.get_by_label('存储桶名称', exact=False).fill('bucket')
    page.get_by_label('访问密钥 ID', exact=True).fill('test-id')
    page.get_by_label('访问密钥', exact=True).fill('test-key')
    page.get_by_role('button', name='保存并验证存储').click()
    expect(page.get_by_text('R2 服务地址不能带桶名路径', exact=False)).to_be_visible()
    expect(page.get_by_label('访问密钥', exact=True)).to_have_value('')
    page.get_by_label('存储桶名称', exact=False).fill('unsaved-bucket')
    dialogs = []
    def keep_editing(dialog):
        dialogs.append(dialog.message)
        dialog.dismiss()
    page.on('dialog', keep_editing)
    page.get_by_role('button', name='稍后配置', exact=True).click()
    expect(page.get_by_label('存储桶名称', exact=False)).to_have_value('unsaved-bucket')
    assert dialogs and '未保存' in dialogs[-1]
    page.remove_listener('dialog', keep_editing)
