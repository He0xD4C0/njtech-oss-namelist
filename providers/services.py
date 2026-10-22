from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.models import SocialAccount
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpResponseRedirect
from django.urls import reverse

from core.crypto import encrypt

from .models import OAuthBinding


class BindingConflict(ValidationError):
    pass


@transaction.atomic
def save_binding(sociallogin, user):
    platform = sociallogin.account.provider
    uid = str(sociallogin.account.uid)
    if platform not in {"github", "gitee", "gitlab"}:
        raise BindingConflict("Unsupported platform")
    # The user lock is shared with repository submit and disconnect operations.
    get_user_model().objects.select_for_update().get(pk=user.pk)
    existing = OAuthBinding.objects.select_for_update().filter(user=user, platform=platform).first()
    if existing and existing.platform_user_id != uid:
        raise BindingConflict("One external identity per platform")
    if OAuthBinding.objects.filter(platform=platform, platform_user_id=uid).exclude(user=user).exists():
        raise BindingConflict("External identity already bound")
    token = sociallogin.token
    if token is None or not token.token:
        if existing:
            return existing
        raise BindingConflict("OAuth access token missing")
    data = sociallogin.account.extra_data
    defaults = {
        "platform_user_id": uid, "username": data.get("username" if platform == "gitlab" else "login") or uid,
        "encrypted_access_token": encrypt(token.token), "expires_at": token.expires_at,
    }
    if token.token_secret or not existing:
        defaults["encrypted_refresh_token"] = encrypt(token.token_secret or "")
    try:
        with transaction.atomic():
            binding, _ = OAuthBinding.objects.update_or_create(user=user, platform=platform, defaults=defaults)
    except IntegrityError:
        raise BindingConflict("External identity already bound") from None
    from repositories.tasks import resume_authorized
    transaction.on_commit(lambda: resume_authorized(user.pk, platform))
    return binding


def connected(request, sociallogin, *, newly_added=False):
    try:
        save_binding(sociallogin, sociallogin.user)
    except BindingConflict:
        # allauth saves SocialAccount before firing this signal. Remove only
        # this failed new account; never touch the winner of a concurrent bind.
        if newly_added:
            SocialAccount.objects.filter(pk=sociallogin.account.pk, user=sociallogin.user).delete()
        if request is not None:
            messages.error(request, "绑定冲突，请先解绑原账号。 / Binding conflict; disconnect the original account first.")
        raise ImmediateHttpResponse(HttpResponseRedirect(reverse("providers:index"))) from None

