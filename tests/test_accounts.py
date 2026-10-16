import json
import re
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import authenticate
from django.contrib.sessions.models import Session
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from core.models import PolicyConsent, PolicyDocument, User
from core.runtime import set_setting

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures('initialized_site')]
PASSWORD = 'Local-identity-password-429!'


@pytest.fixture(autouse=True)
def ready(monkeypatch, settings):
    monkeypatch.setattr('core.forms.verify_turnstile', lambda request: None)
    monkeypatch.setattr('core.turnstile.verify_turnstile', lambda request: None)
    settings.ACCOUNT_RATE_LIMITS = {**settings.ACCOUNT_RATE_LIMITS, 'confirm_email': '100/m/key'}
    for key, value in {'smtp_host': 'mail.test', 'smtp_verified': '1', 'turnstile_site_key': 'test', 'turnstile_secret_key': 'test'}.items():
        set_setting(key, value)
    for kind in ['privacy', 'terms', 'inclusion']:
        PolicyDocument.objects.create(kind=kind, language='zh-CN', source_sha='a' * 40, body='Test')


def account(username='alice', email='alice@example.test', **kwargs):
    user = User.objects.create_user(username, email=email, password=PASSWORD, **kwargs)
    if email:
        EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
    return user


def request_link(client, email, purpose='add'):
    response = client.post(reverse('email_verification_request'), {'purpose': purpose, 'email': email})
    assert response.status_code == 200, response.content
    link = re.search(r'https?://[^\s]+/accounts/confirm-email/[^\s]+', mail.outbox[-1].body).group(0)
    return urlsplit(link).path


def signup_data(email='new@example.test', username='new-user'):
    return {'email': email, 'username': username, 'password1': PASSWORD, 'password2': PASSWORD,
            'policy_language': 'zh-CN', 'policy_versions': json.dumps({'zh-CN': {k: 'a'*40 for k in ['privacy', 'terms', 'inclusion']}}),
            'accept_policies': 'on'}


def test_registration_only_persists_after_email_proof(client):
    data = signup_data()
    assert client.post('/accounts/signup/', data).status_code == 200
    assert not User.objects.exists()
    link = request_link(client, data['email'], 'signup')
    assert not User.objects.exists() and not EmailAddress.objects.exists()
    for row in Session.objects.all():
        assert data['email'] not in str(row.get_decoded())
        assert PASSWORD not in str(row.get_decoded())
    assert Client().get(link).status_code == 200
    assert not User.objects.exists()
    assert Client().post(link).status_code == 200
    assert not User.objects.exists()
    response = client.post('/accounts/signup/', data)
    assert response.status_code == 302, response.context['form'].errors
    user = User.objects.get(username='new-user')
    assert EmailAddress.objects.get(user=user).verified
    assert user.email == data['email']
    assert PolicyConsent.objects.filter(user=user).count() == 1
    assert client.post('/accounts/signup/', {**data, 'username': 'another-user'}).status_code in (200, 302)
    assert User.objects.count() == 1


def test_multi_email_login_default_removal_and_replay(client):
    user = account()
    client.force_login(user)
    link = request_link(client, 'SECOND@Example.test')
    assert EmailAddress.objects.filter(user=user).count() == 1
    assert Client().get(link).context['target_user'].pk == user.pk
    assert EmailAddress.objects.filter(user=user).count() == 1
    assert Client().post(link).context['confirmed']
    user.refresh_from_db()
    assert user.email == 'alice@example.test'
    for name in ('alice', 'ALICE', 'alice@example.test', 'SECOND@example.test'):
        assert authenticate(username=name, password=PASSWORD).pk == user.pk
    assert client.post('/accounts/email/', {'email': 'second@example.test', 'action_primary': '1'}).status_code == 302
    user.refresh_from_db()
    assert user.email == 'second@example.test'
    client.post('/accounts/email/', {'email': user.email, 'action_remove': '1'})
    assert EmailAddress.objects.filter(user=user).count() == 2
    client.post('/accounts/email/', {'email': 'alice@example.test', 'action_remove': '1'})
    assert EmailAddress.objects.filter(user=user).count() == 1
    client.post('/accounts/email/', {'email': user.email, 'action_remove': '1'})
    assert EmailAddress.objects.filter(user=user).count() == 1
    mail.outbox.clear()
    client.logout()
    client.post('/accounts/password/reset/', {'email': 'SECOND@example.test'})
    assert mail.outbox[-1].to == ['second@example.test']
    assert '/accounts/password/reset/key/' in mail.outbox[-1].body


def test_competing_proofs_do_not_reserve_mailbox_or_merge_accounts():
    one, two = account(), account('bravo', 'bravo@example.test')
    a, b = Client(), Client()
    a.force_login(one)
    b.force_login(two)
    first = request_link(a, 'shared@example.test')
    second = request_link(b, 'SHARED@example.test')
    assert not EmailAddress.objects.filter(email='shared@example.test').exists()
    assert b.post(second).context['confirmed']
    response = a.post(first)
    assert response.context['error']
    assert EmailAddress.objects.get(email='shared@example.test').user_id == two.pk
    assert User.objects.count() == 2
    b.post('/accounts/email/', {'email': 'shared@example.test', 'action_remove': '1'})
    assert b.post(second).context['confirmed']
    assert not EmailAddress.objects.filter(email='shared@example.test').exists()


def test_cancel_resend_expiry_mail_failure_and_session_binding(client):
    data = signup_data()
    first = request_link(client, data['email'], 'signup')
    second = request_link(client, data['email'], 'signup')
    assert not Client().post(first).context['proof_email']
    assert Client().post(second).context['confirmed']
    assert not Client().post('/accounts/signup/', data).context['form'].is_valid()
    client.post(reverse('email_verification_request'), {'purpose': 'signup', 'action': 'cancel'})
    assert not Client().post(second).context['proof_email']
    third = request_link(client, data['email'], 'signup')
    cache.clear()
    assert not Client().post(third).context['proof_email']
    with patch('core.mail.send_system_mail', side_effect=OSError('secret SMTP error')):
        response = client.post(reverse('email_verification_request'), {'purpose': 'signup', 'email': data['email']})
    assert response.status_code == 400 and b'secret SMTP' not in response.content
    assert not User.objects.exists() and not EmailAddress.objects.exists()


def test_admin_has_no_email_and_cannot_transfer_or_delete(client):
    admin = User.objects.create_superuser('admin', password=PASSWORD)
    response = client.post('/accounts/login/', {'login': 'admin', 'password': PASSWORD})
    assert response.status_code == 302 and response.url == '/account/profile/'
    assert client.get('/admin/').status_code == 200
    assert client.get('/manage/settings/').status_code == 200
    client.post('/account/delete/', {'password': PASSWORD, 'confirm': 'on'})
    assert User.objects.filter(pk=admin.pk).exists()
    for changes in ({'username': 'other'}, {'is_superuser': False}, {'is_active': False}):
        admin.refresh_from_db()
        for key, value in changes.items():
            setattr(admin, key, value)
        with pytest.raises(ValidationError):
            admin.save()
    with pytest.raises(ValidationError):
        User.objects.create_superuser('another', password=PASSWORD)
    admin.refresh_from_db()
    link = request_link(client, 'admin@example.test')
    client.post(link)
    client.post('/accounts/email/', {'email': 'admin@example.test', 'action_remove': '1'})
    admin.refresh_from_db()
    assert admin.email == '' and not EmailAddress.objects.filter(user=admin).exists()


def test_legacy_account_can_only_enter_recovery_until_verified(client):
    user = account(email='')
    response = client.post('/accounts/login/', {'login': user.username, 'password': PASSWORD})
    assert response.status_code == 302 and response.url == '/accounts/confirm-email/'
    assert '_auth_user_id' not in client.session
    assert client.get(response.url).context['recovery_user'].pk == user.pk
    link = request_link(client, 'recovered@example.test', 'recover')
    assert Client().post(link).context['confirmed']
    assert client.get('/accounts/confirm-email/').url == '/accounts/login/'
    response = client.post('/accounts/login/', {'login': user.username, 'password': PASSWORD})
    assert response.status_code == 302 and response.url == '/account/profile/'


def test_database_constraints_and_identity_read_only(client):
    user = account()
    with pytest.raises(IntegrityError), transaction.atomic():
        EmailAddress.objects.create(user=user, email='pending@example.test', verified=False)
    other = account('bravo', 'bravo@example.test')
    with pytest.raises(IntegrityError), transaction.atomic():
        EmailAddress.objects.create(user=other, email='ALICE@example.test', verified=True)
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.filter(pk=user.pk).update(username='changed')
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.filter(pk=user.pk).update(is_superuser=True)
    client.force_login(user)
    client.post('/account/profile/', {'username': 'changed', 'full_name': 'New name', 'language': 'en'})
    user.refresh_from_db()
    assert user.username == 'alice' and user.full_name == 'New name'
    response = client.get('/accounts/email/')
    assert '邮箱管理'.encode() in response.content or b'Email addresses' in response.content
    assert b'action_send' not in response.content


def test_only_admin_can_grant_and_revoke_staff(client):
    root = User.objects.create_superuser('admin', password=PASSWORD)
    user = account()
    target = reverse('admin:core_user_change', args=[user.pk])
    client.force_login(root)
    data = {'full_name': 'Alice', 'language': 'en', 'quota': 64, 'is_active': 'on', 'is_staff': 'on', '_save': 'Save'}
    assert client.post(target, data).status_code == 302
    user.refresh_from_db()
    assert user.is_staff and not user.is_superuser
    staff = Client()
    staff.force_login(user)
    assert staff.post(target, {**data, 'is_superuser': 'on'}).status_code == 403
    del data['is_staff']
    assert client.post(target, data).status_code == 302
    user.refresh_from_db()
    assert not user.is_staff
    assert staff.get('/admin/').status_code == 302


def test_public_profile_and_email_login_use_binding_source(client):
    user = account(public_email=True)
    # A stale compatibility value must never become a login or public address.
    User.objects.filter(pk=user.pk).update(email='stale@example.test')
    assert authenticate(username='stale@example.test', password=PASSWORD) is None
    assert authenticate(username='ALICE@example.test', password=PASSWORD).pk == user.pk
    response = client.get('/u/alice/')
    assert b'alice@example.test' in response.content and b'stale@example.test' not in response.content
    User.objects.filter(pk=user.pk).update(public_email=False)
    assert b'alice@example.test' not in client.get('/u/alice/').content
    mail.outbox.clear()
    client.post('/accounts/password/reset/', {'email': 'stale@example.test'})
    assert all('/accounts/password/reset/key/' not in message.body for message in mail.outbox)


def test_password_reset_does_not_overwrite_new_default_or_role(client):
    from core.adapters import AccountAdapter

    user = account()
    stale = User.objects.get(pk=user.pk)
    EmailAddress.objects.create(user=user, email='new-default@example.test', verified=True)
    client.force_login(user)
    client.post('/accounts/email/', {'email': 'new-default@example.test', 'action_primary': '1'})
    User.objects.filter(pk=user.pk).update(is_staff=True)
    AccountAdapter().set_password(stale, 'New-local-password-429!')
    user.refresh_from_db()
    assert user.email == 'new-default@example.test' and user.is_staff
    assert user.check_password('New-local-password-429!')


def test_csrf_confirmation_and_tampered_target(client):
    user, other = account(), account('other', 'other@example.test')
    client.force_login(user)
    link = request_link(client, 'next@example.test')
    device = Client(enforce_csrf_checks=True)
    assert device.get(link).status_code == 200
    assert device.post(link).status_code == 403
    csrf = device.cookies['csrftoken'].value
    assert device.post(link, {'csrfmiddlewaretoken': csrf, 'user_id': other.pk, 'purpose': 'signup'}).context['confirmed']
    assert EmailAddress.objects.get(email='next@example.test').user_id == user.pk


def test_postgres_two_confirmations_race_for_one_mailbox(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import connection, connections

    if connection.vendor != 'postgresql':
        pytest.skip('Mailbox concurrency requires PostgreSQL')
    one, two = account(), account('other', 'other@example.test')
    clients = [Client(), Client()]
    for client, user in zip(clients, [one, two], strict=True):
        client.force_login(user)
    links = [request_link(client, 'race@example.test') for client in clients]
    barrier = Barrier(2)
    save = EmailAddress.save

    def simultaneous_insert(self, *args, **kwargs):
        if not self.pk and self.email == 'race@example.test':
            barrier.wait(timeout=10)
        return save(self, *args, **kwargs)

    monkeypatch.setattr(EmailAddress, 'save', simultaneous_insert)

    def confirm(link):
        try:
            response = Client().post(link)
            return '邮箱已确认。'.encode() in response.content
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, links))
    assert sorted(results) == [False, True]
    assert EmailAddress.objects.filter(email='race@example.test').count() == 1
    assert User.objects.count() == 2


def test_postgres_switch_remove_and_confirm_share_user_lock():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import connection, connections

    if connection.vendor != 'postgresql':
        pytest.skip('Mailbox concurrency requires PostgreSQL')
    user = account()
    EmailAddress.objects.create(user=user, email='second@example.test', verified=True)
    clients = [Client() for _ in range(3)]
    for client in clients:
        client.force_login(user)
    link = request_link(clients[2], 'third@example.test')
    barrier = Barrier(3)

    def operation(index):
        try:
            barrier.wait(timeout=10)
            if index == 2:
                return clients[index].post(link).status_code
            return clients[index].post('/accounts/email/', {'email': 'second@example.test',
                'action_primary' if index == 0 else 'action_remove': '1'}).status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(operation, range(3))) == [302, 302, 200]
    user.refresh_from_db()
    primary = EmailAddress.objects.get(user=user, primary=True)
    assert user.email == primary.email and primary.verified
    assert EmailAddress.objects.filter(user=user, email='third@example.test').exists()
    assert EmailAddress.objects.filter(user=user).count() >= 2
