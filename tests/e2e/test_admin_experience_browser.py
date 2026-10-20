import re
from unittest.mock import patch

import pytest
from django.urls import reverse
from playwright.sync_api import expect

from bulletins.models import Announcement
from core.models import SyncRun, User
from core.runtime import get_setting
from repositories.models import AuditDispatch, AuditResult, Repository, Submission

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.e2e, pytest.mark.usefixtures("initialized_site")]


@pytest.fixture
def management_content(session_login, page, live_server):
    manager = User.objects.create_superuser("admin", password="Isolated-browser-password-42", language="en")
    member = User.objects.create_user("member", password="Isolated-browser-password-42")
    repository = Repository.objects.create(
        platform="github", platform_id="1", name="project", full_name="member/project", owner_id="1",
        html_url="https://github.com/member/project", clone_url="https://github.com/member/project.git",
    )
    claim = Submission.objects.create(user=member, repository=repository, status="needs_review", reason="许可证需要处理 / License needs attention")
    result = AuditResult.objects.create(
        submission=claim, generation=1, source_sha="a" * 40, policy_sha="b" * 40,
        decision="needs_review", detector_version="test", evidence={"许可证": "需要更新策略", "路径": "LICENSE"},
    )
    SyncRun.objects.create(kind="documents", status="queue-failed", message="测试任务服务不可用 / Test task service unavailable")
    session_login(manager)
    page.context.add_cookies([{"name": "django_language", "value": "en", "url": live_server.url}])
    return claim, result


@pytest.mark.parametrize("width,theme", [(1440, "light"), (390, "dark")])
def test_chinese_management_navigation_and_configuration(page, live_server, management_content, artifact_dir, width, theme):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(live_server.url + "/admin/")
    page.get_by_label("配色模式", exact=True).select_option(theme)
    expect(page.locator("html")).to_have_attribute("lang", "zh-hans")
    expect(page.locator('[name="language"]')).to_have_count(0)
    expect(page.get_by_role("heading", name="管理首页", exact=True)).to_be_visible()
    if width == 1440:
        expect(page.get_by_text("搜索管理功能…", exact=True)).to_be_visible()
    expect(page.locator(".admin-overview-card").filter(has_text="待处理申请").get_by_text("1", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(artifact_dir / f"management-home-{width}-{theme}.png"), full_page=True)
    page.goto(live_server.url + "/manage/settings/#documents")
    expect(page.get_by_label("运营主体名称", exact=True)).to_be_visible()
    expect(page.locator("#smtp")).not_to_have_attribute("open", "")
    page.get_by_label("运营主体名称", exact=True).fill("本地验收运营者")
    page.locator("#documents").get_by_role("button", name="保存", exact=True).click()
    expect(page.get_by_label("运营主体名称", exact=True)).to_have_value("本地验收运营者")
    expect(page.get_by_text("配置已保存。", exact=True)).to_be_visible()
    assert get_setting("operator_name") == "本地验收运营者"
    navigation = page.get_by_role("navigation", name="配置分组")
    navigation.get_by_role("link", name="邮件服务", exact=True).click()
    expect(page.get_by_label("邮件服务器地址", exact=True)).to_be_visible()
    page.get_by_label("邮件服务器地址", exact=True).fill("invalid://hostname")
    page.get_by_label("测试收件邮箱", exact=True).fill("recipient@example.test")
    with patch("core.views.send_smtp_test") as send:
        page.get_by_role("button", name="保存并测试邮件", exact=True).click()
        expect(page.get_by_text("只填写服务器主机名，不要包含协议或端口。", exact=True)).to_be_visible()
        send.assert_not_called()
    expect(page.get_by_label("邮件服务器地址", exact=True)).to_have_value("invalid://hostname")
    page.get_by_label("邮件服务器地址", exact=True).fill("smtp.example.test")
    page.get_by_label("发件邮箱", exact=True).fill("sender@example.test")
    with patch("core.views.send_smtp_test") as send:
        page.get_by_role("button", name="保存并测试邮件", exact=True).click()
        expect(page.get_by_text("邮件服务器已接受测试邮件，请检查收件箱。", exact=True)).to_be_visible()
        send.assert_called_once_with("recipient@example.test")
    assert get_setting("smtp_host") == "smtp.example.test"
    page.screenshot(path=str(artifact_dir / f"management-settings-{width}-{theme}.png"), full_page=True)
    navigation.get_by_role("link", name="任务状态", exact=True).click()
    expect(page.get_by_text("同步政策正文 · 任务未能提交，请重试；持续失败时请检查任务服务。", exact=True)).to_be_visible()
    expect(page.locator("#task-status")).not_to_contain_text("Test task service unavailable")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors
    page.goto(live_server.url + "/")
    expect(page.locator("html")).to_have_attribute("lang", "en")
    expect(page.locator("#ui-language")).to_have_value("en")


def test_search_evidence_recheck_and_announcement_editor(page, live_server, management_content, artifact_dir):
    claim, result = management_content
    search_requests = []
    page.on("request", lambda request: search_requests.append(request.url) if "/admin/search/?" in request.url else None)
    page.goto(live_server.url + "/admin/")
    page.get_by_text("搜索管理功能…", exact=True).click()
    page.get_by_placeholder("输入关键词搜索", exact=True).fill("收录申请")
    result_link = page.locator("#command-results-list").get_by_role("link").filter(has_text="收录申请")
    expect(result_link).to_be_visible()
    expect(page.locator("#command-results-note")).to_contain_text("找到")
    assert len(search_requests) == 1, "A completed search must not trigger another search request"
    result_link.click()
    expect(page.locator("#result_list")).not_to_contain_text("License needs attention")
    search = page.locator('#searchbar')
    search.fill("member/project")
    search.press("Enter")
    page.get_by_role("link", name="GitHub · member/project", exact=True).click()
    expect(page.get_by_text("许可证需要处理", exact=True)).to_be_visible()
    expect(page.locator("#content")).not_to_contain_text("License needs attention")
    page.locator(f'a[href="{reverse("admin:repositories_auditresult_change", args=[result.pk])}"]').click()
    expect(page.locator(".admin-evidence")).to_contain_text('"路径": "LICENSE"')
    page.screenshot(path=str(artifact_dir / "management-evidence.png"), full_page=True)
    page.goto(live_server.url + "/admin/repositories/submission/")
    page.locator('input[name="_selected_action"]').check()
    page.locator('select[name="action"]').select_option("recheck")
    with patch("repositories.tasks.audit_submission.delay") as dispatch:
        page.locator('button[name="index"]').click()
        expect(page.get_by_text(re.compile("已为 1 条申请安排重新机审"))).to_be_visible()
        dispatch.assert_called_once()
    claim.refresh_from_db()
    assert claim.status == "needs_review"
    assert AuditDispatch.objects.filter(submission=claim, generation=2).exists()
    page.goto(live_server.url + "/admin/bulletins/announcement/add/")
    expect(page.get_by_role("textbox", name="标题*", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="加粗", exact=True)).to_be_visible()
    page.get_by_role("textbox", name="标题*", exact=True).fill("后台体验验收公告")
    page.locator(".CodeMirror").click()
    page.keyboard.type("本地验收正文")
    page.get_by_role("button", name="预览", exact=True).click()
    expect(page.locator(".editor-preview-active")).to_contain_text("本地验收正文")
    expect(page.get_by_placeholder("日期", exact=True)).to_be_visible()
    page.get_by_role("button", name="保存", exact=True).click()
    expect(page.locator("#result_list").get_by_role("link", name="后台体验验收公告", exact=True)).to_be_visible()
    assert Announcement.objects.get(title="后台体验验收公告").body == "本地验收正文"
