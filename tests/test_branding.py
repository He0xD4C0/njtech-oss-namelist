import base64
from io import BytesIO

import pytest
from allauth.account.models import EmailAddress
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from core.branding import LogoForm, logo_url, save_logo
from core.mail import send_system_mail
from core.models import PolicyDocument, SiteState, User
from core.runtime import get_setting, set_setting

pytestmark = pytest.mark.django_db


def image_bytes(color=(30, 60, 90, 128)):
    output = BytesIO()
    Image.new('RGBA', (800, 400), color).save(output, 'PNG')
    return output.getvalue()


def upload(data=None):
    return SimpleUploadedFile('brand.png', data or image_bytes(), content_type='image/png')


def ready_setup():
    user = User.objects.create_superuser('admin', email='logo@example.test', password='test-only-password')
    EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    SiteState.objects.update_or_create(pk=1, defaults={'activated_at': None, 'bootstrap_user': user})
    for key, value in {'smtp_host': 'smtp.example.test', 'smtp_verified': '1', 'turnstile_site_key': 'site', 'turnstile_secret_key': 'secret'}.items():
        set_setting(key, value)
    for kind in ('privacy', 'terms', 'inclusion'):
        PolicyDocument.objects.create(kind=kind, language='zh-CN', body='policy')
    return user


def test_logo_normalizes_and_preserves_transparency():
    form = LogoForm({}, {'logo': upload()})
    assert form.is_valid(), form.errors
    save_logo(form.cleaned_data['logo'])
    result = Image.open(BytesIO(base64.b64decode(get_setting('site_logo_data'))))
    assert result.size == (512, 256) and result.mode == 'RGBA'
    assert result.getpixel((1, 1))[3] == 128


@pytest.mark.parametrize('data', [b'<svg onload="alert(1)"></svg>', b'not an image', b'x' * (5*1024*1024+1)])
def test_invalid_upload_retains_previous_logo(data):
    save_logo(image_bytes())
    previous = logo_url()
    form = LogoForm({}, {'logo': upload(data)})
    assert not form.is_valid() and 'logo' in form.errors
    assert logo_url() == previous


def test_setup_upload_and_public_asset_are_precise_exceptions(client, settings):
    ready_setup()
    payload = {'action': 'configure', 'step': 'operations', 'task': 'branding', 'group': 'branding', 'logo': upload()}
    client.post('/setup/', payload)
    assert not logo_url()
    client.post('/setup/', {'action': 'authorize', 'secret': settings.BOOTSTRAP_SECRET})
    payload['logo'] = upload()
    assert client.post('/setup/', payload).status_code == 302
    current = logo_url()
    response = client.get(current)
    assert response.status_code == 200 and response['Content-Type'] == 'image/png'
    assert 'no-store' in response['Cache-Control']
    assert client.post(current).status_code == 405
    assert client.get('/site-logo/not-current.png').status_code == 404
    assert client.get('/not-an-exception/').status_code == 302
    page = client.get('/setup/?step=operations&task=branding', HTTP_ACCEPT='application/json').json()
    assert page['brand_logo_url'] == current and current in page['html']
    send_system_mail('Test', 'Body', ['test@example.test'])
    assert settings.SITE_BASE_URL.rstrip('/') + current in mail.outbox[-1].alternatives[0].content


def test_admin_upload_requires_superuser_and_updates_unfold(client, initialized_site):
    user = User.objects.create_user('normal', email='normal@example.test', password='test-password')
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    client.force_login(user)
    assert client.post('/manage/settings/', {'action': 'save', 'group': 'branding', 'logo': upload()}).status_code == 403
    assert not logo_url()
    user = User.objects.create_superuser('admin', password='test-password')
    client.force_login(user)
    assert client.post('/manage/settings/', {'action': 'save', 'group': 'branding', 'logo': upload()}).status_code == 302
    current = logo_url()
    for path in ('/', '/submit/', '/admin/', '/admin/core/user/', '/manage/settings/'):
        assert f'<link rel="icon" type="image/png" data-brand-favicon href="{current}">' in client.get(path).content.decode()
    client.logout()
    assert f'data-brand-favicon href="{current}"' in client.get('/accounts/login/').content.decode()
    assert client.get(current)['Cache-Control'] == 'public, max-age=86400, immutable'
    save_logo(image_bytes((255, 20, 10, 255)))
    assert logo_url() != current
    assert client.get(current).status_code == 404
