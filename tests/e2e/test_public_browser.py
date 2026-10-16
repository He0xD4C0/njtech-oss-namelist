import re
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from allauth.account.models import EmailAddress
from django.core import mail
from django.utils import timezone
from playwright.sync_api import expect

from bulletins.models import Announcement, AnnouncementReceipt
from core.models import PolicyConsent, User
from repositories.models import Repository, Submission

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.e2e, pytest.mark.usefixtures("initialized_site")]


def test_operator_settings_render_in_both_policy_languages(page, live_server, session_login):
    from core.models import PolicyDocument, SiteState

    SiteState.objects.update_or_create(pk=1, defaults={"activated_at": timezone.now()})

    user = User.objects.create_superuser('admin', "policy@example.invalid", "Browser-test-password-421")
    EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    for language in ("zh-CN", "en-US"):
        PolicyDocument.objects.create(
            language=language, kind="terms", source_sha="a" * 40,
            body="# Terms\n\n{{ operator_name }}\n\n{{ operator_contact_email }}",
        )
    for language, operator, email in (
        ("zh-CN", "本站运营者", "联系邮箱未设置"),
        ("en-US", "Site operator", "Contact email not set"),
    ):
        page.goto(live_server.url + f"/policies/terms/?language={language}")
        expect(page.locator("article").get_by_text(operator, exact=True)).to_be_visible()
        expect(page.locator("article").get_by_text(email, exact=True)).to_be_visible()
    session_login(user)
    page.goto(live_server.url + "/manage/settings/")
    name = page.get_by_label("运营主体名称", exact=True)
    email = page.get_by_label("公开联系邮箱", exact=True)
    expect(name).to_be_visible()
    expect(email).to_be_visible()
    name.fill("开源社区 [test](https://example.org)")
    email.fill("help@example.org")
    page.locator("#documents").get_by_role("button", name="保存", exact=True).click()
    expect(name).to_have_value("开源社区 [test](https://example.org)")
    expect(email).to_have_value("help@example.org")
    for language in ("zh-CN", "en-US"):
        for width in (1440, 390):
            page.set_viewport_size({"width": width, "height": 900})
            page.goto(live_server.url + f"/policies/terms/?language={language}")
            expect(page.locator("article").get_by_text("开源社区 [test](https://example.org)", exact=True)).to_be_visible()
            expect(page.locator("article").get_by_text("help@example.org", exact=True)).to_be_visible()
            expect(page.locator('article a[href="https://example.org"]')).to_have_count(0)
    page.goto(live_server.url + "/manage/settings/")
    name.fill("")
    email.fill("")
    page.locator("#documents").get_by_role("button", name="保存", exact=True).click()
    page.goto(live_server.url + "/policies/terms/?language=zh-CN")
    expect(page.locator("article").get_by_text("本站运营者", exact=True)).to_be_visible()
    expect(page.locator("article").get_by_text("联系邮箱未设置", exact=True)).to_be_visible()


def test_backup_frequency_save_does_not_enable_unconfigured_backup(page, live_server, session_login, monkeypatch):
    from core.runtime import get_setting
    from core.tasks import restore_drill

    monkeypatch.setenv("SITE_ENVIRONMENT", "staging")
    monkeypatch.setenv("BACKUP_ENABLED", "1")
    monkeypatch.setenv("BACKUP_S3_BUCKET", "")
    user = User.objects.create_superuser('admin', "backup@example.invalid", "Browser-test-password-421")
    EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    session_login(user)
    page.goto(live_server.url + "/manage/settings/")
    expect(page.get_by_text("需要服务器的独立备份配置先就绪", exact=False)).to_be_visible()
    page.get_by_label("恢复演练频率").select_option("quarterly")
    page.locator("#operations").get_by_role("button", name="保存", exact=True).click()
    expect(page.get_by_label("恢复演练频率")).to_have_value("quarterly")
    assert get_setting("restore_schedule") == "quarterly"
    with patch("core.tasks.subprocess.run") as run, patch("core.tasks.record_failure") as failure:
        assert restore_drill() == "not-configured"
        run.assert_not_called()
        failure.assert_not_called()


@pytest.fixture
def public_content():
    user = User.objects.create_user(
        "alice", email="private@example.com", password="Visible-testing-password-42", full_name="张同学"
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    for number in range(23):
        now = timezone.now()
        repo = Repository.objects.create(
            platform="github",
            platform_id=str(number),
            name=f"project-{number}",
            full_name=f"alice/project-{number}",
            owner_id="1",
            html_url=f"https://github.com/alice/project-{number}",
            clone_url=f"https://github.com/alice/project-{number}.git",
            description="面向开发者的开放工具，所有代码公开并以 MIT 许可证分发。",
            stars=40 + number,
            archived=number == 22,
            commit_at=now - timedelta(days=number),
            synced_at=now,
        )
        Submission.objects.create(
            user=user,
            repository=repo,
            status="published",
            published_at=now,
            baseline_at=now,
            baseline_stars=repo.stars,
            license_labels=["MIT"],
        )
    return user


def test_public_desktop_mobile_filters_and_source(page, live_server, public_content, artifact_dir):
    failures = []
    page.on("pageerror", lambda error: failures.append(str(error)))
    response = page.goto(live_server.url + "/")
    assert response.status == 200
    expect(page.get_by_role("heading", name="最新收录", exact=True)).to_be_visible()
    expect(page.locator(".repository-card")).to_have_count(20)
    assert page.locator(".repository-card a").first.get_attribute("href").startswith("https://github.com/")
    assert not page.get_by_text(public_content.email, exact=True).count()
    assert (
        page.locator(".repository-grid").first.evaluate(
            "node => getComputedStyle(node).gridTemplateColumns.split(' ').length"
        )
        == 3
    )
    page.screenshot(path=str(artifact_dir / "home-desktop.png"), full_page=True)
    page.get_by_label("配色模式").select_option("dark")
    contrast = page.locator(".repository-card").first.evaluate("""card => {
      const ctx = document.createElement('canvas').getContext('2d');
      const luminance = color => {
        ctx.fillStyle = color; ctx.fillRect(0, 0, 1, 1);
        const rgb = [...ctx.getImageData(0, 0, 1, 1).data].slice(0, 3).map(x => {
          x /= 255; return x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4;
        });
        return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
      };
      const foreground = luminance(getComputedStyle(card.querySelector('.card-footer a')).color);
      const background = luminance(getComputedStyle(card).backgroundColor);
      return (Math.max(foreground, background) + .05) / (Math.min(foreground, background) + .05);
    }""")
    assert contrast >= 4.5
    page.screenshot(path=str(artifact_dir / "home-desktop-dark.png"), full_page=True)
    page.get_by_label("配色模式").select_option("auto")
    print(f"desktop screenshot: {artifact_dir / 'home-desktop.png'}")
    page.get_by_role("link", name="仓库", exact=True).click()
    expect(page.locator(".repository-card")).to_have_count(20)
    page.get_by_label("archived", exact=True).select_option("1")
    page.get_by_role("button", name="筛选", exact=True).click()
    expect(page.locator(".repository-card")).to_have_count(1)
    assert "archived=1" in page.url
    page.get_by_role("link", name="重置", exact=True).click()
    page.get_by_role("link", name="下一页", exact=False).click()
    expect(page.locator(".repository-card")).to_have_count(3)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(live_server.url + "/")
    expect(page.get_by_role("link", name="探索仓库", exact=False)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert (
        page.locator(".repository-grid").first.evaluate(
            "node => getComputedStyle(node).gridTemplateColumns.split(' ').length"
        )
        == 1
    )
    page.screenshot(path=str(artifact_dir / "home-mobile-full.png"), full_page=True)
    page.screenshot(path=str(artifact_dir / "home-mobile.png"))
    print(f"mobile screenshot: {artifact_dir / 'home-mobile.png'}")
    page.goto(live_server.url + "/rankings/")
    expect(page.get_by_role("heading", name="GitHub", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="Gitee", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="GitLab.com", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.goto(live_server.url + "/source/")
    expect(page.get_by_role("status")).to_contain_text("源码仓库地址尚未配置")
    page.goto(live_server.url + "/u/alice/")
    expect(page.locator(".profile-avatar")).to_be_visible()
    assert page.locator(".profile-avatar").evaluate("image => image.complete && image.naturalWidth > 0")
    assert not page.get_by_text(public_content.email, exact=True).count()
    assert not failures


def test_policy_nondismissible_all_pages_and_normal_cross_device(
    page, live_server, public_content, session_login, artifact_dir
):
    Announcement.objects.create(title="普通提示", body="一次性提示", creator_name="管理员")
    policy = Announcement.objects.create(
        title="重要政策更新",
        body="# 请阅读\n\n" + "更新说明。\n\n" * 40,
        kind="policy",
        creator_name="管理员",
        editors=[{"id": 1, "name": "管理员"}],
    )
    session_login(public_content)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(live_server.url + "/")
    dialog = page.locator("dialog[open]")
    expect(dialog).to_contain_text("重要政策更新")
    expect(dialog.get_by_role("button", name="关闭公告")).to_have_count(0)
    page.keyboard.press("Escape")
    expect(dialog).to_be_visible()
    page.mouse.click(4, 4)
    expect(dialog).to_be_visible()
    page.screenshot(path=str(artifact_dir / "policy-mobile.png"))
    print(f"policy screenshot: {artifact_dir / 'policy-mobile.png'}")
    page.goto(live_server.url + "/repositories/")
    expect(page.locator("dialog[open]")).to_contain_text("重要政策更新")
    page.locator("dialog[open]").get_by_role("button", name="我已阅读并确认").click()
    expect(page.locator("dialog[open]")).to_contain_text("普通提示")
    page.locator("dialog[open]").get_by_role("button", name="关闭公告").click()
    expect(page.locator("dialog[open]")).to_have_count(0)
    page.reload()
    expect(page.locator("dialog[open]")).to_have_count(0)
    assert (
        AnnouncementReceipt.objects.get(announcement=policy, user=public_content).confirmed_revision
        == policy.revision
    )
    policy.body = "新正文"
    policy.save()
    page.goto(live_server.url + "/rankings/")
    expect(page.locator("dialog[open]")).to_contain_text("新正文")
    # A stale modal must not confirm newly changed text.
    policy.body = "再次更新"
    policy.save()
    page.locator("dialog[open]").get_by_role("button", name="我已阅读并确认").click()
    expect(page.locator("dialog[open] [role=alert]")).to_contain_text("公告已更新")
    expect(page.locator("dialog[open]")).to_be_visible()
    policy.expires_at = timezone.now() - timedelta(seconds=1)
    policy.save()
    page.reload()
    expect(page.locator("dialog[open]")).to_have_count(0)


def test_real_admin_popup_and_server_markdown_editor(page, live_server, session_login):
    user = User.objects.create_user(
        "staff-user", email="staff@example.com", password="Visible-testing-password-42", is_staff=True
    )
    EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    notice = Announcement.objects.create(
        title="管理员也需确认", body="Policy text", kind="policy", creator_name="管理员"
    )
    session_login(user)
    for path in ("/admin/", "/admin/bulletins/", "/admin/bulletins/announcement/"):
        page.goto(live_server.url + path)
        expect(page.locator("dialog[open]")).to_contain_text("管理员也需确认")
        page.keyboard.press("Escape")
        expect(page.locator("dialog[open]")).to_be_visible()
    page.locator("dialog[open]").get_by_role("button", name="我已阅读并确认").click()
    expect(page.locator("dialog[open]")).to_have_count(0)
    page.goto(live_server.url + f"/admin/bulletins/announcement/{notice.pk}/change/")
    expect(page.locator(".CodeMirror")).to_be_visible()
    page.locator(".CodeMirror").click()
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.insert_text('**服务端预览** <script>alert("bad")</script>')
    page.locator(".editor-toolbar button.preview").click()
    expect(page.locator(".editor-preview strong")).to_have_text("服务端预览")
    assert page.locator(".editor-preview script").count() == 0


def test_registration_verification_and_password_login_browser(page, browser, live_server, external_services_test_only):
    page.goto(live_server.url + "/accounts/signup/")
    expect(page.locator(".cf-turnstile")).to_have_attribute("data-sitekey", "test-only-sitekey")
    page.get_by_label("政策阅读语言 / Policy language").select_option("en-US")
    expect(page.locator("[data-policy-link]").first).to_have_attribute("href", re.compile(r"language=en-US"))
    page.locator("#id_email").fill("new-user@example.com")
    page.locator("#id_username").fill("new-user")
    page.locator("#id_password1").fill("Local-browser-safe-password-419")
    page.locator("#id_password2").fill("Local-browser-safe-password-419")
    page.locator("#id_accept_policies").check()
    page.get_by_role("button", name="发送验证邮件", exact=True).click()
    expect(page.locator('[data-mailbox-status]')).to_contain_text("验证邮件已发送")
    assert not User.objects.filter(username="new-user").exists()
    content = mail.outbox[-1].body
    link = re.search(r"https?://[^\s]+/accounts/confirm-email/[^\s]+", content).group(0)
    device = browser.new_context()
    confirmation = device.new_page()
    confirmation.goto(live_server.url + urlsplit(link).path)
    confirmation.get_by_role("button", name="确认此邮箱").click()
    expect(confirmation.get_by_role('status')).to_contain_text('邮箱已确认')
    device.close()
    expect(page.locator('[data-mailbox-status]')).to_contain_text("邮箱已验证", timeout=10000)
    assert not User.objects.filter(username="new-user").exists()
    page.get_by_role("button", name="创建账号", exact=True).click()
    expect(page).to_have_url(re.compile(r"/account/profile/$"))
    user = User.objects.get(username="new-user")
    assert user.has_usable_password()
    assert PolicyConsent.objects.get(user=user).language == "en-US"
    assert EmailAddress.objects.get(user=user).verified
    page.get_by_role("button", name="退出登录", exact=True).click()
    page.goto(live_server.url + "/accounts/login/")
    page.locator("#id_login").fill("new-user@example.com")
    page.locator("#id_password").fill("Local-browser-safe-password-419")
    page.get_by_role("button", name="登录", exact=True).click()
    expect(page).to_have_url(re.compile(r"/account/profile/$"))
    expect(page.get_by_role("link", name="new-user", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="查看我的公开主页", exact=True)).to_be_visible()
    page.get_by_role("button", name="退出登录", exact=True).click()
    expect(page.get_by_role("link", name="登录", exact=True)).to_be_visible()


def test_multiple_mailboxes_visible_in_both_languages(page, browser, live_server, external_services_test_only, session_login, artifact_dir):
    user = User.objects.create_user('mailbox-user', email='first@example.test', password='Mailbox-test-password-41!')
    EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    session_login(user)
    page.goto(live_server.url + '/accounts/email/')
    expect(page.get_by_role('button', name='解绑', exact=True)).to_have_count(0)
    page.get_by_label('新邮箱', exact=True).fill('second@example.test')
    page.get_by_role('button', name='发送验证邮件', exact=True).click()
    expect(page.locator('[data-mailbox-status]')).to_contain_text('验证邮件已发送')
    assert EmailAddress.objects.filter(user=user).count() == 1
    link = re.search(r'/accounts/confirm-email/[^\s]+/', mail.outbox[-1].body).group(0)
    device = browser.new_context()
    confirm = device.new_page()
    confirm.goto(live_server.url + link)
    expect(confirm.get_by_text('@mailbox-user', exact=True)).to_be_visible()
    confirm.get_by_role('button', name='确认此邮箱').click()
    device.close()
    expect(page.locator('[data-mailbox-status]')).to_contain_text('邮箱已绑定', timeout=10000)
    page.get_by_role('link', name='查看邮箱列表', exact=True).click()
    expect(page.get_by_role('button', name='设为默认', exact=True)).to_be_visible()
    page.get_by_role('button', name='设为默认', exact=True).click()
    user.refresh_from_db()
    assert user.email == 'second@example.test'
    for language in ('zh-hans', 'en'):
        page.context.add_cookies([{'name': 'django_language', 'value': language, 'url': live_server.url}])
        page.reload()
        for width in (390, 1440):
            page.set_viewport_size({'width': width, 'height': 950})
            expect(page.get_by_role('button', name='Send verification email' if language == 'en' else '发送验证邮件', exact=True)).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(artifact_dir / f'mailboxes-{language}-{width}.png'), full_page=True)
    page.get_by_role('button', name='Remove', exact=True).click()
    expect(page.get_by_role('button', name='Remove', exact=True)).to_have_count(0)
    assert EmailAddress.objects.filter(user=user).count() == 1
    page.goto(live_server.url + '/account/profile/')
    expect(page.locator('#id_username')).to_be_disabled()
    page.locator('#id_public_email').check()
    page.get_by_role('button', name='Save', exact=True).click()
    page.goto(live_server.url + '/u/mailbox-user/')
    expect(page.get_by_text('second@example.test', exact=True)).to_be_visible()
    expect(page.get_by_text('first@example.test', exact=True)).to_have_count(0)
