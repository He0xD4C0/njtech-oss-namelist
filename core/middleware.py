from ipaddress import ip_address

from allauth.account.models import EmailAddress
from django.conf import settings
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect
from django.utils import translation


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
