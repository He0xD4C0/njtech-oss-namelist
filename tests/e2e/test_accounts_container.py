"""Opt-in accounts acceptance on an isolated, activated DEBUG=0 local stack.

Real PostgreSQL, Redis, SMTP/STARTTLS and server-side Turnstile test-key checks.
The browser supplies Cloudflare's public dummy token, not a challenge bypass in
application code. The site's test canonical hostname must be example.com.
"""
import json
import os
import re
import subprocess
from urllib.parse import urlsplit

import httpx
import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not os.getenv('NAMELIST_CONTAINER_COMPOSE'),
              reason='Isolated accounts container acceptance is opt-in')]
PASSWORD = 'Local-preview-only-acceptance-527!'


def test_real_mailbox_registration_and_two_login_methods(browser, artifact_dir):
    origin = os.environ['NAMELIST_CONTAINER_ORIGIN'].rstrip('/')
    mailbox = os.environ['NAMELIST_CONTAINER_MAILPIT'].rstrip('/')
    compose = os.environ['NAMELIST_CONTAINER_COMPOSE']
    assert urlsplit(origin).hostname in {'localhost', '127.0.0.1'}
    assert urlsplit(mailbox).hostname in {'localhost', '127.0.0.1'}

    def counts():
        code = ('import json; from core.models import User; from allauth.account.models import EmailAddress; '
                'print(json.dumps([User.objects.count(), EmailAddress.objects.count()]))')
        result = subprocess.run(['docker', 'compose', '-f', compose, 'exec', '-T', 'web',
                                 '/app/scripts/entrypoint.sh', 'python', 'manage.py', 'shell', '-c', code],
                                text=True, capture_output=True, check=True, timeout=30)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def challenge(page):
        # Public Cloudflare testing token. Production server still calls siteverify.
        page.locator('form.account-form').evaluate('''form => {
          let input = form.querySelector('[name="cf-turnstile-response"]');
          if (!input) { input = document.createElement('input'); input.type = 'hidden';
            input.name = 'cf-turnstile-response'; form.appendChild(input); }
          input.value = 'XXXX.DUMMY.TOKEN.XXXX';
        }''')

    def login(page, identity):
        page.goto(origin + '/accounts/login/')
        expect(page.get_by_label('用户名或邮箱', exact=True)).to_be_visible()
        page.locator('#id_login').fill(identity)
        page.locator('#id_password').fill(PASSWORD)
        challenge(page)
        page.get_by_role('button', name='登录', exact=True).click()
        expect(page).to_have_url(origin + '/account/profile/', timeout=25000)

    def confirm(email, expected_user=None):
        with httpx.Client(trust_env=False, timeout=15) as http:
            messages = http.get(mailbox + '/api/v1/messages').json()['messages']
            selected = next(m for m in messages if any(t['Address'].lower() == email for t in m['To']))
            message = http.get(mailbox + '/api/v1/message/' + selected['ID']).json()
        assert message['HTML'] and message['Text']
        path = re.search(r'/accounts/confirm-email/[^\s]+/', message['Text']).group(0)
        device = browser.new_context()
        page = device.new_page()
        before = counts()
        page.goto(origin + path)
        assert counts() == before  # GET must never bind.
        if expected_user:
            expect(page.get_by_text('@' + expected_user, exact=True)).to_be_visible()
        page.get_by_role('button', name='确认此邮箱').click()
        expect(page.get_by_text('邮箱已确认。', exact=False)).to_be_visible()
        expect(page.get_by_role('link', name='返回原页面')).to_be_visible()
        device.close()

    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    context.route('https://challenges.cloudflare.com/**', lambda route: route.abort())
    page = context.new_page()
    assert counts() == [1, 0]
    login(page, 'admin')
    expect(page.get_by_role('link', name='注销账号', exact=True)).to_have_count(0)
    page.goto(origin + '/manage/settings/')
    expect(page.get_by_label('测试收件邮箱')).to_be_visible()
    page.goto(origin + '/account/profile/')
    page.get_by_role('button', name='退出登录', exact=True).click()
    page.goto(origin + '/accounts/signup/')
    page.locator('#id_email').fill('identity-first@example.test')
    page.locator('#id_username').fill('identity-user')
    page.locator('#id_password1').fill(PASSWORD)
    page.locator('#id_password2').fill(PASSWORD)
    page.locator('#id_accept_policies').check()
    challenge(page)
    page.get_by_role('button', name='发送验证邮件', exact=True).click()
    expect(page.locator('[data-mailbox-status]')).to_contain_text('验证邮件已发送', timeout=25000)
    assert counts() == [1, 0]
    confirm('identity-first@example.test')
    expect(page.locator('[data-mailbox-status]')).to_contain_text('邮箱已验证', timeout=12000)
    assert counts() == [1, 0]
    challenge(page)
    page.get_by_role('button', name='创建账号', exact=True).click()
    expect(page).to_have_url(origin + '/account/profile/', timeout=25000)
    assert counts() == [2, 1]
    page.goto(origin + '/accounts/email/')
    page.get_by_label('新邮箱').fill('identity-second@example.test')
    page.get_by_role('button', name='发送验证邮件', exact=True).click()
    expect(page.locator('[data-mailbox-status]')).to_contain_text('验证邮件已发送', timeout=20000)
    assert counts() == [2, 1]
    confirm('identity-second@example.test', 'identity-user')
    expect(page.locator('[data-mailbox-status]')).to_contain_text('邮箱已绑定', timeout=12000)
    page.get_by_role('link', name='查看邮箱列表').click()
    assert counts() == [2, 2]
    page.get_by_role('button', name='设为默认', exact=True).click()
    for language in ('zh-hans', 'en'):
        context.add_cookies([{'name': 'django_language', 'value': language, 'url': origin}])
        page.reload()
        for width in (390, 1440):
            page.set_viewport_size({'width': width, 'height': 1000})
            expect(page.get_by_role('button', name='Send verification email' if language == 'en' else '发送验证邮件')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(artifact_dir / f'container-mailboxes-{language}-{width}.png'), full_page=True)
    context.add_cookies([{'name': 'django_language', 'value': 'zh-hans', 'url': origin}])
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.goto(origin + '/account/profile/')
    page.get_by_role('button', name='退出登录', exact=True).click()
    for identity in ('identity-user', 'IDENTITY-FIRST@example.test', 'identity-second@example.test'):
        login(page, identity)
        page.get_by_role('button', name='退出登录', exact=True).click()
    login(page, 'identity-user')
    page.goto(origin + '/accounts/email/')
    page.get_by_role('button', name='解绑', exact=True).click()
    expect(page.get_by_role('button', name='解绑', exact=True)).to_have_count(0)
    assert counts() == [2, 1]
    (artifact_dir / 'accounts-container-result.json').write_text(json.dumps({
        'database': 'PostgreSQL 17', 'cache': 'Redis 8', 'smtp': 'Mailpit STARTTLS',
        'admin': 'no email', 'creation_before_confirmation': False, 'binding_on_get': False,
        'login': ['username', 'all bound emails', 'case insensitive email'],
        'languages': ['zh-hans', 'en'], 'viewports': [390, 1440],
        'oauth': 'covered by transport-mocked tests; no live provider credentials',
        'turnstile': 'real server verification of public test key/token; browser challenge UI not tested',
    }, ensure_ascii=False, indent=2))
    context.close()
