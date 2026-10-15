from ipaddress import ip_address

from allauth.account.models import EmailAddress
from django.conf import settings
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect
from django.urls import Resolver404, resolve, reverse
from django.utils import translation
from django.utils.cache import add_never_cache_headers

from .models import SiteState


class SiteInitializationMiddleware:
    """Keep an unactivated installation closed before any business view runs."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path_info.startswith(settings.STATIC_URL):
            return self.get_response(request)
        state = SiteState.objects.filter(pk=1).only("activated_at", "bootstrap_user_id").first()
        request.site_initializing = not (state and state.activated_at)
        if not request.site_initializing:
            response = self.get_response(request)
            if request.path_info == reverse("core:setup"):
                add_never_cache_headers(response)
            return response
        try:
            match = resolve(request.path_info)
        except Resolver404:
            match = None
        allowed = match and match.view_name in {"core:setup", "set_language"}
        if match and match.view_name == "account_confirm_email" and state and state.bootstrap_user_id:
            from allauth.account.models import get_emailconfirmation_model

            confirmation = get_emailconfirmation_model().from_key(match.kwargs["key"])
            allowed = bool(confirmation and confirmation.email_address.user_id == state.bootstrap_user_id)
            if not allowed:
                response = redirect("/setup/?step=mail&confirmation=invalid")
                add_never_cache_headers(response)
                return response
        response = self.get_response(request) if allowed else redirect("core:setup")
        add_never_cache_headers(response)
        return response


class ProxyHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.TRUST_PROXY_HEADERS:
            value = request.META.get("HTTP_X_REAL_IP", request.META.get("REMOTE_ADDR", ""))
            try:
                request.META["HTTP_X_REAL_IP"] = str(ip_address(value))
            except ValueError:
                return HttpResponseBadRequest("Expected a single client IP address")
        return self.get_response(request)


class AccountReadinessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            if request.user.language and not request.COOKIES.get("django_language"):
                translation.activate(request.user.language)
                request.LANGUAGE_CODE = request.user.language
            protected = request.path.startswith(
                ("/account/", "/platforms/", "/submit/", "/manage/", "/admin/")
            )
            if protected:
                if not request.user.has_usable_password():
                    return redirect("account_set_password")
                if not EmailAddress.objects.filter(
                    user=request.user, email__iexact=request.user.email, verified=True
                ).exists():
                    return redirect("account_email")
        return self.get_response(request)
