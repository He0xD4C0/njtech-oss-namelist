from io import BytesIO
from unittest.mock import patch

import pytest
from allauth.account.models import EmailAddress
from django.core.cache import cache
from django.utils import timezone
from PIL import Image
from playwright.sync_api import expect

from core.models import PolicyDocument, SiteState, User
from core.runtime import set_setting

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.e2e]


@pytest.mark.parametrize("width", [390, 768, 1440])
@pytest.mark.parametrize("theme", ["light", "dark", "auto"])
def test_optional_backup_setup(page, live_server, settings, monkeypatch, artifact_dir, session_login, width, theme):
    monkeypatch.setenv("BACKUP_CONFIG_MODE", "application")
    page.set_viewport_size({"width": width, "height": 1000})
    page.add_init_script(f"localStorage.setItem('adminTheme', JSON.stringify('{theme}'));")
    user = User.objects.create_superuser("admin", email="backup@example.test")
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    SiteState.objects.create(pk=1, bootstrap_user=user)
    for key, value in {"smtp_host": "smtp.example.test", "smtp_verified": "1", "turnstile_site_key": "site", "turnstile_secret_key": "secret"}.items():
        set_setting(key, value)
    for kind in ("privacy", "terms", "inclusion"):
        PolicyDocument.objects.create(kind=kind, language="zh-CN", body="policy")
    cache.set("backup-readiness", {"checked_at": timezone.now().isoformat(), "state": "not-configured"})
    page.goto(live_server.url + "/setup/")
    page.get_by_label("服务器初始化密钥").fill(settings.BOOTSTRAP_SECRET)
    page.get_by_role("button", name="验证并开始").click()
    expect(page.get_by_role("heading", name="创建初始管理员", exact=True)).to_be_visible()
    page.goto(live_server.url + "/setup/?step=operations&task=branding")
    image = BytesIO()
    Image.new("RGBA", (128, 128), (50, 90, 130, 128)).save(image, "PNG")
    page.get_by_label("选择新的 Logo", exact=True).set_input_files({"name": "brand.png", "mimeType": "image/png", "buffer": image.getvalue()})
    page.get_by_role("button", name="上传并应用 Logo", exact=True).click()
    expect(page.get_by_alt_text("当前站点 Logo", exact=True)).to_be_visible()
    expect(page.locator('[data-brand-logo]')).to_be_visible()
    page.wait_for_function("() => { const image = document.querySelector('[data-brand-logo]'); return image?.complete && image.naturalWidth > 0; }")
    favicon = page.locator('link[rel="icon"]')
    original_icon = page.locator('[data-brand-logo]').get_attribute('src')
    expect(favicon).to_have_attribute('href', original_icon)
    icon_response = page.request.get(live_server.url + original_icon)
    assert icon_response.ok and icon_response.headers['content-type'] == 'image/png'
    assert Image.open(BytesIO(icon_response.body())).size == (128, 128)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(artifact_dir / f"logo-{width}-{theme}.png"), full_page=True)
    page.goto(live_server.url + "/setup/?step=operations&task=backup")
    expect(page.get_by_role("heading", name="数据库备份", exact=True)).to_be_visible()
    expect(page.get_by_text("尚无数据库备份保护，可稍后配置。", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="启用自动备份", exact=True)).to_be_disabled()
    for key, value in {"endpoint": "https://backup.example.invalid/bucket", "bucket": "backups", "region": "auto", "access_key": "test-access", "secret_key": "test-secret"}.items():
        page.locator(f'[name="{key}"]').fill(value)
    page.get_by_role("button", name="保存并测试连接", exact=True).click()
    expect(page.get_by_text("填写 HTTPS 服务地址，桶名单独填写", exact=False)).to_be_visible()
    expect(page.locator('[name="secret_key"]')).to_have_value("")
    page.locator('[name="endpoint"]').fill("https://backup.example.invalid")
    page.locator('[name="access_key"]').fill("test-access")
    page.locator('[name="secret_key"]').fill("test-secret")
    with patch("core.backup.backup_operation.apply_async") as send:
        page.get_by_role("button", name="保存并测试连接", exact=True).click()
        expect(page.get_by_text("正在测试 S3 写入、读取、列举和删除权限。", exact=True)).to_be_visible()
        assert send.call_count == 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(artifact_dir / f"backup-{width}-{theme}.png"), full_page=True)
    page.get_by_role("button", name="稍后配置", exact=True).click()
    expect(page.get_by_role("heading", name="设置运维选项", exact=True)).to_be_visible()

    page.goto("about:blank")  # Stop page reads before mutating the SQLite fixture.
    SiteState.objects.filter(pk=1).update(activated_at=timezone.now())
    user.is_staff = user.is_superuser = True
    user.set_password("test-only-password")
    user.save()
    session_login(user)
    page.goto(live_server.url + "/manage/settings/#branding")
    image = BytesIO()
    Image.new("RGBA", (128, 128), (100, 60, 150, 255)).save(image, "PNG")
    page.get_by_label("选择新的 Logo", exact=True).set_input_files({"name": "updated.png", "mimeType": "image/png", "buffer": image.getvalue()})
    page.get_by_role("button", name="上传并应用 Logo", exact=True).click()
    expect(page.get_by_alt_text("当前站点 Logo", exact=True)).to_be_visible()
    updated_icon = page.get_by_alt_text("当前站点 Logo", exact=True).get_attribute('src')
    assert updated_icon != original_icon
    expect(favicon).to_have_attribute('href', updated_icon)
    for path in ('/admin/', '/', '/submit/'):
        page.goto(live_server.url + path)
        expect(favicon).to_have_attribute('href', updated_icon)
    page.goto(live_server.url + "/manage/settings/#branding")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator('#branding').screenshot(path=str(artifact_dir / f"logo-admin-{width}-{theme}.png"))
