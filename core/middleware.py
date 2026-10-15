from allauth.account.models import EmailAddress
from django.shortcuts import redirect
from django.utils import translation


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
