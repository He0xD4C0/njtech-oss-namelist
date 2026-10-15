import re

from allauth.socialaccount.models import SocialAccount
from allauth.socialaccount.providers.oauth2.client import OAuth2Error
from allauth.socialaccount.providers.oauth2.views import OAuth2CallbackView, OAuth2LoginView
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django_ratelimit.decorators import ratelimit

from core.runtime import provider_enabled
from core.turnstile import verify_turnstile


def social_login_from_profile(adapter, request, response):
    response.raise_for_status()
    try:
        data = response.json()
        uid = str(data["id"])
        username = data["username" if adapter.provider_id == "gitlab" else "login"]
        if not re.fullmatch(r"[1-9][0-9]{0,19}", uid) or not isinstance(username, str) or not username:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise OAuth2Error("Invalid platform identity response") from None
    # Lock users BEFORE allauth.lookup writes SocialAccount, in the same order
    # used by unlink/delete. Otherwise reauthorization and unlink can deadlock.
    user_ids = set(SocialAccount.objects.filter(provider=adapter.provider_id, uid=uid).values_list("user_id", flat=True))
    if request.user.is_authenticated:
        user_ids.add(request.user.pk)
    list(get_user_model().objects.select_for_update().filter(pk__in=user_ids).order_by("pk"))
    return adapter.get_provider().sociallogin_from_response(request, data)


class LoginView(OAuth2LoginView):
    @method_decorator(ratelimit(key="ip", rate="10/m", method="POST", block=True))
    def dispatch(self, request, *args, **kwargs):
        if not provider_enabled(self.adapter.provider_id):
            raise Http404("Platform is not configured")
        if request.method == "POST" and not request.user.is_authenticated:
            try:
                verify_turnstile(request)
            except ValidationError as exc:
                return render(request, "socialaccount/login.html", {
                    "provider": self.get_provider(), "process": request.GET.get("process", "login"),
                    "security_error": " ".join(exc.messages),
                }, status=400)
        return super().dispatch(request, *args, **kwargs)


class CallbackView(OAuth2CallbackView):
    def dispatch(self, request, *args, **kwargs):
        if not provider_enabled(self.adapter.provider_id):
            raise Http404("Platform is not configured")
        try:
            # allauth saves SocialAccount before emitting the binding signal.
            # Keep account, credentials and registration consent atomic, even
            # when two callback requests race for the same external identity.
            with transaction.atomic():
                return super().dispatch(request, *args, **kwargs)
        except IntegrityError:
            messages.error(request, "外部身份已绑定，请使用原账号或重新授权。 / This identity is already bound; use the original account or reauthorize.")
            return redirect("providers:index" if request.user.is_authenticated else "account_login")

