"""Page-wide checks of rendered controls, without hover, in both themes and viewport sizes."""
import json

import pytest
from allauth.account.forms import default_token_generator
from allauth.account.models import EmailAddress
from allauth.account.utils import user_pk_to_url_str
from django.contrib import admin
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone
from playwright.sync_api import expect

from bulletins.models import Announcement, AnnouncementReceipt
from core.crypto import encrypt
from core.models import PolicyDocument, SiteState, SyncRun, User
from core.runtime import set_setting
from providers.models import OAuthBinding
from repositories.models import Repository, Submission

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.e2e]

CONTROL_AUDIT = """() => {
  const canvas = document.createElement('canvas').getContext('2d');
  const rgb = color => {
    canvas.clearRect(0, 0, 1, 1); canvas.fillStyle = color; canvas.fillRect(0, 0, 1, 1);
    return [...canvas.getImageData(0, 0, 1, 1).data].map(x => x / 255);
  };
  const mix = (front, back) => front.slice(0, 3).map((x, i) => x * front[3] + back[i] * (1-front[3]));
  const background = node => {
    if (!node) return [1, 1, 1];
    return mix(rgb(getComputedStyle(node).backgroundColor), background(node.parentElement));
  };
  const lum = color => color.map(x => x <= .04045 ? x/12.92 : ((x+.055)/1.055)**2.4)
    .reduce((sum, x, i) => sum + x * [.2126, .7152, .0722][i], 0);
  const contrast = (a, b) => (Math.max(lum(a),lum(b))+.05)/(Math.min(lum(a),lum(b))+.05);
  const elements = [...document.querySelectorAll('button, a.btn, a.admin-action, .badge, input:not([type="hidden"]), select, textarea, summary, #site-navigation a, #account-navigation a')];
  return elements.filter(n => n.checkVisibility() && !n.disabled && n.getAttribute('aria-hidden') !== 'true')
    .filter(n => !n.closest('.CodeMirror') && !n.classList.contains('select2-search__field'))
    .map(n => {
      const style = getComputedStyle(n), bg = background(n), surrounding = background(n.parentElement);
      const border = mix(rgb(style.borderTopColor), bg);
      const rect = n.getBoundingClientRect();
      return {tag:n.tagName, type:n.type, id:n.id, classes:n.className, text:(n.innerText || n.getAttribute('aria-label') || n.name || '').trim().slice(0,80),
        foreground:style.color, background:style.backgroundColor, border:style.borderTopColor,
        textContrast:contrast(mix(rgb(style.color), bg), bg), borderContrast:parseFloat(style.borderTopWidth) ? contrast(border,surrounding):0,
        fillContrast:contrast(bg,surrounding), height:rect.height, width:rect.width};
    });
}"""


def test_all_page_controls(page, live_server, session_login, artifact_dir, settings):
    SiteState.objects.update_or_create(pk=1, defaults={'activated_at': timezone.now()})
    user = User.objects.create_superuser('admin', 'theme-audit@example.test', 'Local-theme-audit-password-42')
    member = User.objects.create_user('member', 'member@example.test', 'Local-theme-audit-password-42')
    for person in (user, member):
        EmailAddress.objects.create(user=person, email=person.email, primary=True, verified=True)
    EmailAddress.objects.create(user=member, email='second@example.test', verified=True)
    for language in ('zh-CN', 'en-US'):
        for kind in ('privacy', 'terms', 'inclusion'):
            PolicyDocument.objects.create(kind=kind, language=language, body=f'# {kind}\n\nTest policy.', source_sha='a'*40)
    for key, value in {'smtp_host':'local.invalid', 'smtp_verified':'1', 'turnstile_site_key':'test-site-key',
                       'turnstile_secret_key':'test-secret', 'oauth_github_client_id':'test-client',
                       'oauth_github_client_secret':'test-secret'}.items():
        set_setting(key, value)
    notice = Announcement.objects.create(title='界面审查公告', body='## 更新说明\n\n本地测试正文。', creator_name='管理员')
    for person in (user, member):
        AnnouncementReceipt.objects.create(user=person, announcement=notice, confirmed_revision=notice.revision, delivered_at=timezone.now())
    repo = Repository.objects.create(platform='github', platform_id='12', name='example', full_name='member/example',
                                     owner_id='7', html_url='https://github.com/member/example',
                                     clone_url='https://github.com/member/example.git')
    Submission.objects.create(user=member, repository=repo, status='published', published_at=timezone.now(),
                                       baseline_at=timezone.now(), baseline_stars=0)
    OAuthBinding.objects.create(user=member, platform='github', platform_user_id='7', username='member',
                                encrypted_access_token=encrypt('test-token'))
    SyncRun.objects.create(kind='documents', status='queue-failed', message='Test queue unavailable')
    failures = []
    page.on('pageerror', lambda error: failures.append(str(error)))
    observations = []

    def visit(path, group):
        response = page.goto(live_server.url + path)
        expected_status = 401 if path == '/accounts/3rdparty/login/error/' else 200
        assert response.status == expected_status, (path, response.status)
        if group == 'setup':
            expect(page.locator('#setup-flow')).to_have_attribute('data-url', path)
        if not page.locator('[data-theme-select]').count():
            (artifact_dir / 'missing-theme.html').write_text(page.content())
            page.screenshot(path=str(artifact_dir / 'missing-theme.png'), full_page=True)
            raise AssertionError(f'Missing theme selector: {path}')
        for width in (1440, 390):
            page.set_viewport_size({'width':width, 'height':1000})
            for theme in ('light', 'dark'):
                page.locator('[data-theme-select]').select_option(theme)
                expect(page.locator('html')).to_have_attribute('data-bs-theme', theme)
                page.mouse.move(0, 0)
                page.wait_for_function("document.getAnimations().every(a => a.playState !== 'running' || a.effect.getTiming().iterations === Infinity)")
                controls = page.evaluate(CONTROL_AUDIT)
                overflow = page.evaluate('document.documentElement.scrollWidth > innerWidth')
                slug = (group + path.split('?')[0]).strip('/').replace('/', '-')
                if '?' in path:
                    slug += '-' + path.split('task=')[-1]
                screenshot = f'{slug}-{theme}-{width}.png'
                page.screenshot(path=str(artifact_dir / screenshot), full_page=True)
                observations.append({'path':path, 'resolved_url':page.url, 'group':group, 'width':width, 'theme':theme,
                                     'overflow':overflow, 'controls':controls, 'screenshot':screenshot})
                (artifact_dir / 'page-controls.json').write_text(json.dumps(observations, ensure_ascii=False, indent=2))

    for path in ('/', '/repositories/', '/rankings/', '/announcements/', f'/announcements/{notice.pk}/',
                 '/source/', '/u/member/', '/accounts/login/', '/accounts/signup/', '/accounts/password/reset/',
                 '/accounts/password/reset/done/', '/accounts/password/reset/key/done/',
                 '/accounts/confirm-email/invalid/', '/policies/privacy/', '/policies/terms/', '/policies/inclusion/',
                 '/accounts/github/login/', '/accounts/3rdparty/login/error/', '/accounts/3rdparty/login/cancelled/',
                 '/accounts/password/reset/key/invalid-token/'):
        visit(path, 'public')
    visit(reverse('account_reset_password_from_key', kwargs={
        'uidb36': user_pk_to_url_str(member), 'key': default_token_generator.make_token(member),
    }), 'public')
    session_login(member)
    for path in ('/account/profile/', '/account/delete/', '/platforms/', '/submit/', '/accounts/email/',
                 '/accounts/password/change/', '/accounts/logout/'):
        visit(path, 'member')
    page.goto('about:blank')
    member.set_unusable_password()
    member.save(update_fields=['password'])
    session_login(member)
    visit('/accounts/password/set/', 'member')
    session_login(user)
    request = RequestFactory().get('/admin/')
    request.user = user
    admin_paths = ['/manage/settings/', '/admin/']
    for model, model_admin in admin.site._registry.items():
        prefix = f'admin:{model._meta.app_label}_{model._meta.model_name}'
        if model_admin.has_view_permission(request):
            admin_paths.append(reverse(prefix + '_changelist'))
            obj = model.objects.first()
            if obj:
                admin_paths.append(reverse(prefix + '_change', args=[obj.pk]))
        if model_admin.has_add_permission(request):
            admin_paths.append(reverse(prefix + '_add'))
    for path in dict.fromkeys(admin_paths):
        visit(path, 'admin')
    page.goto('about:blank')
    # Keep the failed-sync recovery state in admin coverage, then unlock the
    # later setup tasks so their screenshots cannot silently repeat documents.
    SyncRun.objects.filter(kind='documents').update(status='success')
    SiteState.objects.filter(pk=1).update(activated_at=None, bootstrap_user=user)
    visit('/setup/', 'setup-access')
    page.get_by_label('服务器初始化密钥').fill(settings.BOOTSTRAP_SECRET)
    page.get_by_role('button', name='验证并开始').click()
    expect(page.get_by_role('heading', name='创建初始管理员', exact=True)).to_be_visible()
    for step, tasks in [('mail',['smtp','admin']), ('requirements',['turnstile','documents','licenses']),
                        ('integrations',['github','gitee','gitlab','storage']),
                        ('operations',['branding','backup','operations']), ('review',['review'])]:
        for task in tasks:
            visit(f'/setup/?step={step}&task={task}', 'setup')
    (artifact_dir / 'page-controls.json').write_text(json.dumps(observations, ensure_ascii=False, indent=2))
    assert not failures, failures
    assert not [(o['path'],o['theme'],o['width']) for o in observations if o['overflow']]
    low_contrast = [(o['path'], o['theme'], c['text'], round(c['textContrast'], 2))
                    for o in observations for c in o['controls']
                    if (c['tag'] in {'BUTTON', 'A', 'SUMMARY'} or 'badge' in c['classes'].split())
                    and c['text'] and c['textContrast'] < 4.5]
    assert not low_contrast, low_contrast
    print(f'Inspected {len(observations)} page/theme/viewport combinations: {artifact_dir / "page-controls.json"}')
