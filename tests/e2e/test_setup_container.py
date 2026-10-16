"""Opt-in acceptance against an explicitly isolated local DEBUG=0 stack.

NAMELIST_CONTAINER_ORIGIN=http://localhost:18002
NAMELIST_CONTAINER_MAILPIT=http://localhost:18029
The caller owns fixture setup/teardown; this test never clears a database.
"""
import json
import os
import time
from urllib.parse import urlsplit

import httpx
import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not os.getenv('NAMELIST_CONTAINER_ORIGIN'), reason='Local container acceptance is opt-in')]


def test_debug_zero_complete_setup(browser, artifact_dir):
    origin = os.environ['NAMELIST_CONTAINER_ORIGIN'].rstrip('/')
    mailbox = os.environ['NAMELIST_CONTAINER_MAILPIT'].rstrip('/')
    assert urlsplit(origin).hostname in {'localhost', '127.0.0.1'}
    assert urlsplit(mailbox).hostname in {'localhost', '127.0.0.1'}
    with httpx.Client(trust_env=False, timeout=15) as http:
        for asset, content_type in [('js/setup.js', 'text/javascript'), ('css/site.css', 'text/css')]:
            response = http.get(origin + '/static/' + asset)
            assert response.status_code == 200
            assert content_type in response.headers['content-type']
            assert 'public' in response.headers['cache-control']
        assert http.get(origin + '/').status_code == 302
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    page = context.new_page()
    page.goto(origin + '/setup/')
    page.get_by_label('服务器初始化密钥').fill('local-development-bootstrap')
    page.get_by_role('button', name='验证并开始').click()
    expect(page.get_by_role('heading', name='连接邮件服务', exact=True)).to_be_visible()
    page.get_by_label('邮件服务器地址', exact=True).fill('mailpit')
    page.get_by_label('端口', exact=True).fill('1025')
    page.get_by_label('连接后升级加密（STARTTLS）', exact=True).check()
    page.get_by_label('发件邮箱', exact=True).fill('preview@example.invalid')
    page.screenshot(path=str(artifact_dir / 'container-smtp-desktop.png'), full_page=True)
    started = time.monotonic()
    page.get_by_role('button', name='保存并发送测试邮件').click()
    expect(page.get_by_role('heading', name='创建初始管理员', exact=True)).to_be_visible(timeout=20000)
    smtp_seconds = time.monotonic() - started
    expect(page.get_by_label('用户名', exact=True)).to_have_value('admin')
    expect(page.get_by_label('用户名', exact=True)).to_be_disabled()
    # The sole admin is deliberately created without any mailbox.
    page.get_by_label('登录密码', exact=True).fill('Local-preview-only-acceptance-527!')
    page.get_by_role('button', name='创建超管', exact=True).click()
    expect(page.get_by_role('heading', name='配置安全验证', exact=True)).to_be_visible()
    # Public Turnstile test keys; challenge integration itself is covered separately.
    page.get_by_label('站点公钥', exact=False).fill('1x00000000000000000000AA')
    page.get_by_label('验证密钥', exact=False).fill('1x0000000000000000000000000000000AA')
    page.get_by_role('button', name='保存并继续').click()
    page.get_by_label('政策正文仓库').fill('https://github.com/He0xD4C0/njtech-oss-eula.git')
    page.get_by_role('button', name='保存并同步').click()
    # Public Git transport may retry; this is not a page-response timing assertion.
    expect(page.get_by_text('本次同步已完成', exact=True)).to_be_visible(timeout=240000)
    page.get_by_role('link', name='继续', exact=True).click()
    for _ in range(7):
        page.get_by_role('button', name='稍后配置', exact=True).click()
    expect(page.get_by_label('恢复演练频率')).to_be_visible()
    page.get_by_label('恢复演练频率').select_option('off')
    page.get_by_role('button', name='保存并继续').click()
    expect(page.get_by_role('button', name='完成激活并开放站点')).to_be_enabled()
    for width in (390, 768, 1440):
        page.set_viewport_size({'width': width, 'height': 900})
        page.screenshot(path=str(artifact_dir / f'container-review-{width}.png'), full_page=True)
    page.get_by_role('button', name='完成激活并开放站点').click()
    expect(page).to_have_url(origin + '/accounts/login/')
    with httpx.Client(trust_env=False, timeout=15) as http:
        assert http.get(origin + '/setup/').status_code == 404
        assert http.get(origin + '/').status_code == 200
    (artifact_dir / 'container-result.json').write_text(json.dumps({
        'origin': origin, 'smtp': 'real local STARTTLS Mailpit', 'smtp_ui_seconds': round(smtp_seconds, 3),
        'admin': 'created without email', 'policies': 'real public Git fetch via Celery',
        'activation': 'passed', 'public_smtp_delivery': 'not tested', 'real_turnstile_oauth_s3': 'not tested',
    }, indent=2))
    context.close()
