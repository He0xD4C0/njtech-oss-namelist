from smtplib import SMTPAuthenticationError
from unittest.mock import patch

import pytest
from django.core import mail
from django.test import Client
from django.utils.translation import override

from core.adapters import AccountAdapter
from core.forms import ConfigurationForm
from core.mail import send_system_mail
from core.models import SourceSyncState, SyncRun, User
from core.runtime import get_setting, set_setting
from core.tasks import sync_documents

pytestmark = pytest.mark.django_db


def authorized(client, settings):
    client.post('/setup/', {'action': 'authorize', 'secret': settings.BOOTSTRAP_SECRET})


def smtp_data(**extra):
    return {'action': 'test-smtp', 'step': 'mail', 'task': 'smtp', 'group': 'smtp',
            'smtp_host': 'smtp.example.test', 'smtp_port': '994', 'smtp_security': 'tls',
            'smtp_username': 'sender@example.test', 'smtp_password': 'private-app-password',
            'smtp_from_email': 'sender@example.test', 'recipient': 'test@example.test', **extra}


def test_smtp_precedes_account_and_does_not_create_one(client, settings):
    authorized(client, settings)
    account = {'action': 'account', 'username': 'admin', 'email': '', 'password': 'Good-password-for-initial-537!'}
    response = client.post('/setup/', account)
    assert 'task=smtp' in response.url
    assert not User.objects.exists()
    response = client.post('/setup/', smtp_data(), HTTP_ACCEPT='application/json')
    assert response.json()['redirect'].endswith('task=admin')
    assert get_setting('smtp_verified') == '1'
    assert get_setting('smtp_port') == '994'
    assert not User.objects.exists()
    assert mail.outbox[-1].to == ['test@example.test']
    assert mail.outbox[-1].alternatives[0].mimetype == 'text/html'
    assert client.post('/setup/', account).status_code == 302
    assert User.objects.count() == 1
    assert User.objects.get().is_superuser


def test_smtp_failure_is_actionable_and_preserves_configuration(client, settings):
    authorized(client, settings)
    with patch('core.views.send_smtp_test', side_effect=SMTPAuthenticationError(535, b'private-server-reply')):
        response = client.post('/setup/', smtp_data(), HTTP_ACCEPT='application/json')
    html = response.json()['html']
    assert '邮箱认证失败' in html and '配置已保存' in html
    assert 'private-server-reply' not in html and 'private-app-password' not in html
    assert get_setting('smtp_password') == 'private-app-password'
    assert get_setting('smtp_host') == 'smtp.example.test'
    assert get_setting('smtp_verified') == ''
    assert not User.objects.exists()
    response = client.post('/setup/', smtp_data(smtp_password=''))
    assert response.status_code == 302
    assert get_setting('smtp_password') == 'private-app-password'
    # A changed configuration must be tested again before creating the administrator.
    set_setting('smtp_port', '465')
    assert 'task=smtp' in client.get('/setup/?step=mail&task=admin').url


def test_recipient_validation_and_status_do_not_leak_or_write(client, settings):
    authorized(client, settings)
    response = client.post('/setup/', smtp_data(recipient='bad-address'))
    assert response.status_code == 200
    assert not mail.outbox
    assert get_setting('smtp_host') == ''
    client.post('/setup/', smtp_data())
    before = (User.objects.count(), SyncRun.objects.count(), len(mail.outbox))
    status = client.get('/setup/?action=status')
    assert status.json()['authorized'] is True
    assert 'no-store' in status['Cache-Control']
    assert b'private-app-password' not in status.content and b'test@example.test' not in status.content
    assert before == (User.objects.count(), SyncRun.objects.count(), len(mail.outbox))
    unauthorized = Client().get('/setup/?action=status')
    assert unauthorized.status_code == 403 and unauthorized.json() == {'authorized': False}
    fresh = Client()
    authorized(fresh, settings)
    response = fresh.get('/setup/?step=mail&task=smtp')
    assert response.context['form']['smtp_port'].value() == '994'
    assert response.context['recipient_form']['recipient'].value() == 'sender@example.test'


@pytest.mark.parametrize('suffix', ['', '.git'])
def test_repository_urls_explain_and_accept_suffix(suffix):
    with override('zh-hans'):
        form = ConfigurationForm({'license_source_url': 'https://github.com/example/rules' + suffix}, group='licenses')
        assert form.is_valid()
        assert form.cleaned_data['license_source_url'] == 'https://github.com/example/rules.git'
        assert '带不带 .git' in form.fields['license_source_url'].help_text
        assert 'whitelist/' in form.fields['license_source_url'].help_text


def test_r2_endpoint_correction_and_custom_gateway_paths():
    for endpoint, valid in [('https://account.r2.cloudflarestorage.com/bucket', False),
                            ('https://account.r2.cloudflarestorage.com', True),
                            ('https://s3.example.test/gateway/s3', True)]:
        form = ConfigurationForm({'s3_endpoint': endpoint, 's3_bucket': 'bucket'}, group='storage')
        assert form.is_valid() is valid
        if not valid:
            assert '桶名' in str(form.errors['s3_endpoint'])
        elif '.r2.' in endpoint:
            assert form.cleaned_data['s3_region'] == 'auto'


def test_language_and_security_choices_preserve_custom_port():
    set_setting('smtp_security', 'tls')
    set_setting('smtp_port', '994')
    with override('en'):
        form = ConfigurationForm(group='smtp')
        assert form.fields['smtp_host'].label == 'SMTP host'
        assert form['smtp_port'].value() == '994'
        assert 'Direct encryption' in form.fields['smtp_security'].choices[0][1]
    with override('zh-hans'):
        assert ConfigurationForm(group='smtp').fields['smtp_host'].label == '邮件服务器地址'
    form = ConfigurationForm({'smtp_security': 'none', 'smtp_host': 'smtp.example.test'}, group='smtp')
    assert not form.is_valid()


def test_unified_mail_template_escapes_content_and_retains_text(settings):
    settings.SITE_BASE_URL = 'http://10.126.126.4'
    send_system_mail('Task <script>bad</script>', '<img src=x onerror=bad()>\nSecond line', ['test@example.test'],
                     action_url='http://10.126.126.4/manage/settings/', action_label='View status')
    message = mail.outbox[-1]
    html = message.alternatives[0].content
    assert '<script>' not in html and '<img src=x' not in html
    assert '&lt;img' in html and 'View status' in html
    assert 'Second line' in message.body
    assert 'http://10.126.126.4' in message.body
    assert message.message().get_content_type() == 'multipart/alternative'


@pytest.mark.parametrize('template,context,button', [
    ('email_confirmation_signup', {'activate_url': 'http://localhost/accounts/confirm-email/key/'}, '确认此邮箱'),
    ('password_reset_key', {'password_reset_url': 'http://localhost/accounts/password/reset/key/'}, '重置密码'),
    ('password_changed', {}, None),
    ('email_changed', {'from_email': 'old@example.test', 'to_email': 'new@example.test'}, None),
])
def test_account_mail_reuses_single_layout(template, context, button):
    from django.contrib.sites.models import Site
    user = User.objects.create_user('mail-user', email='mail@example.test')
    context.update(user=user, current_site=Site.objects.get_current(), request=None)
    with override('zh-hans'):
        message = AccountAdapter().render_mail('account/email/' + template, user.email, context)
    assert len(message.alternatives) == 1
    html = message.alternatives[0].content
    assert 'N/O &nbsp; NJTech OSS' in html
    assert message.body.strip()
    if button:
        assert button in html
        url = context.get('activate_url') or context['password_reset_url']
        assert message.body.count(url) == 1
        assert html.count(url) == 3  # button href and one fallback link (href + text)
        assert 'Email地址' not in message.subject


def test_sync_updates_submitted_run_and_rejects_superseded_generation():
    from django.utils import timezone

    from core.git import GitSnapshot
    set_setting('policy_source_url', 'https://github.com/example/policies.git')
    generation = SourceSyncState.objects.get(kind='documents').generation
    run = SyncRun.objects.create(kind='documents', status='queued')
    files = {f'zh-CN/{kind}.md': 'policy' for kind in ('privacy', 'terms', 'inclusion')}
    with patch('core.git.fetch_snapshot', return_value=GitSnapshot('a' * 40, timezone.now(), files)):
        sync_documents(run_id=run.pk, source=get_setting('policy_source_url'), generation=generation)
    run.refresh_from_db()
    assert run.status == 'success' and run.source_sha == 'a' * 40
    assert SyncRun.objects.filter(kind='documents', status='success').count() == 1
    old = SyncRun.objects.create(kind='documents', status='queued')
    set_setting('policy_source_url', 'https://github.com/example/new-policies.git')
    with patch('core.git.fetch_snapshot') as fetch:
        assert sync_documents(run_id=old.pk, source='https://github.com/example/policies.git', generation=generation) == 'superseded'
        fetch.assert_not_called()


def test_transient_sync_retries_keep_run_identity(monkeypatch):
    from celery.exceptions import Retry

    source = 'https://github.com/example/policies.git'
    set_setting('policy_source_url', source)
    generation = SourceSyncState.objects.get(kind='documents').generation
    run = SyncRun.objects.create(kind='documents', status='queued')
    monkeypatch.setattr(sync_documents.request, 'called_directly', False)
    monkeypatch.setattr(sync_documents.request, 'retries', 0)
    with patch('core.git.fetch_snapshot', side_effect=ConnectionError), patch.object(sync_documents, 'retry', side_effect=Retry) as retry:
        with pytest.raises(Retry):
            sync_documents.run(run_id=run.pk, source=source, generation=generation)
        assert retry.call_args.kwargs['kwargs']['run_id'] == run.pk
    run.refresh_from_db()
    assert run.status == 'retrying'
    monkeypatch.setattr(sync_documents.request, 'retries', 4)
    with patch('core.git.fetch_snapshot', side_effect=ConnectionError), patch('core.tasks.notify_failure.delay'):
        with pytest.raises(ConnectionError):
            sync_documents.run(run_id=run.pk, source=source, generation=generation, business_retries=4)
    run.refresh_from_db()
    assert run.status == 'failed'
    assert SyncRun.objects.filter(kind='documents').count() == 2  # source change + this operation


def test_duplicate_delivery_does_not_repeat_a_running_sync():
    import uuid
    from datetime import timedelta

    from django.utils import timezone
    source = 'https://github.com/example/policies.git'
    set_setting('policy_source_url', source)
    generation = SourceSyncState.objects.get(kind='documents').generation
    run = SyncRun.objects.create(kind='documents', status='running', execution_token=uuid.uuid4(),
                                 lease_expires_at=timezone.now() + timedelta(seconds=120))
    with patch('core.git.fetch_snapshot') as fetch:
        assert sync_documents(run_id=run.pk, source=source, generation=generation) == 'running'
        fetch.assert_not_called()
