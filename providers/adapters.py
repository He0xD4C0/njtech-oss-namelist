from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from allauth.socialaccount.providers.base import AuthProcess
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponseRedirect
from django.urls import reverse

from core.crypto import decrypt, encrypt
from core.runtime import get_setting, registration_ready

from .models import PLATFORMS, OAuthBinding

SCOPES = {"github": ["read:user"], "gitee": ["user_info"], "gitlab": ["read_user"]}


class SocialAdapter(DefaultSocialAccountAdapter):
    def list_apps(self, request, provider=None, client_id=None):
        # Do not consult plaintext allauth SocialApp/config credentials. Each app
        # is transient, decrypted only for an OAuth request and never saved.
        apps = []
        for platform, name in PLATFORMS:
            if provider and provider != platform:
                continue
            configured_id = get_setting(f"oauth_{platform}_client_id")
            secret = get_setting(f"oauth_{platform}_client_secret")
            if not configured_id or not secret or (client_id and configured_id != client_id):
                continue
            apps.append(SocialApp(provider=platform, name=name, client_id=configured_id,
                                  secret=secret, settings={"scope": SCOPES[platform], "verified_email": False,
                                                          **({"gitlab_url": "https://gitlab.com"} if platform == "gitlab" else {})}))
        return apps

    def is_open_for_signup(self, request, sociallogin):
        return registration_ready()

    def is_email_verified(self, provider, email):
        return False

    def can_authenticate_by_email(self, login, email):
        # Identity is platform + immutable ID, never a provider's email assertion.
        return False

    def pre_social_login(self, request, sociallogin):
        for address in sociallogin.email_addresses:
            address.verified = False
        platform, uid = sociallogin.account.provider, str(sociallogin.account.uid)
        if platform not in SCOPES or not uid:
            raise ImmediateHttpResponse(HttpResponseRedirect(reverse("providers:index")))
        if sociallogin.state.get("process") == AuthProcess.CONNECT and request.user.is_authenticated:
            conflict = OAuthBinding.objects.filter(user=request.user, platform=platform).exclude(platform_user_id=uid).exists()
            conflict |= SocialAccount.objects.filter(user=request.user, provider=platform).exclude(uid=uid).exists()
            conflict |= OAuthBinding.objects.filter(platform=platform, platform_user_id=uid).exclude(user=request.user).exists()
            if conflict or (sociallogin.is_existing and sociallogin.user.pk != request.user.pk):
                messages.error(request, "每个平台只能绑定一个账号，且外部身份不能跨本站账号绑定。 / This platform or identity is already bound.")
                raise ImmediateHttpResponse(HttpResponseRedirect(reverse("providers:index")))

    def serialize_instance(self, instance):
        data = super().serialize_instance(instance)
        if isinstance(instance, SocialToken):
            # Pending social signup / reauthentication otherwise saves plaintext
            # access & refresh tokens to Django's database-backed session.
            data["token"] = encrypt(data.get("token") or "")
            data["token_secret"] = encrypt(data.get("token_secret") or "")
            data["_encrypted_oauth"] = True
        return data

    def deserialize_instance(self, model, data):
        if model is SocialToken and data is not None:
            data = dict(data)
            if not data.pop("_encrypted_oauth", False):
                raise ValueError("Unencrypted pending OAuth credentials are not accepted")
            from cryptography.fernet import InvalidToken
            try:
                data["token"] = decrypt(data.get("token") or "")
                data["token_secret"] = decrypt(data.get("token_secret") or "")
            except InvalidToken:
                raise ValueError("Pending OAuth credentials cannot be decrypted") from None
        return super().deserialize_instance(model, data)

    @transaction.atomic
    def save_user(self, request, sociallogin, form=None):
        # The account adapter owns password and PolicyConsent creation. Calling
        # it exactly once via allauth avoids duplicate consent records.
        for address in sociallogin.email_addresses:
            address.verified = False
        user = super().save_user(request, sociallogin, form=form)
        from .services import BindingConflict, save_binding
        try:
            save_binding(sociallogin, user)
        except BindingConflict:
            messages.error(request, "外部身份已绑定，请登录原账号。 / This external identity is already bound.")
            raise ImmediateHttpResponse(HttpResponseRedirect(reverse("account_login"))) from None
        from core.services import import_oauth_profile
        data = sociallogin.account.extra_data
        import_oauth_profile(user, data.get("name") or "", data.get("avatar_url") or "")
        return user

    def get_connect_redirect_url(self, request, socialaccount):
        return reverse("providers:index")

