from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.urls import include, path

from core import authentication, email_verification
from providers.views import connections_compat

admin.site.login = login_required(admin.site.login)
urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/signup/", authentication.SignupView.as_view(), name="account_signup"),
    path("accounts/3rdparty/signup/", authentication.SocialSignupView.as_view(), name="socialaccount_signup"),
    path("accounts/email/", email_verification.manage_emails, name="account_email"),
    path("accounts/confirm-email/", email_verification.recovery, name="account_email_verification_sent"),
    path("accounts/confirm-email/<str:key>/", email_verification.confirm_email, name="account_confirm_email"),
    path("accounts/email-verification/request/", email_verification.request_verification, name="email_verification_request"),
    path("accounts/email-verification/status/", email_verification.verification_status, name="email_verification_status"),
    path("accounts/3rdparty/", connections_compat, name="socialaccount_connections"),
    path("accounts/", include("allauth.urls")),
    path("i18n/", include("django.conf.urls.i18n")),
    path("", include("core.urls")),
    path("platforms/", include("providers.urls")),
    path("submit/", include("repositories.urls")),
    path("announcements/", include("bulletins.urls")),
    path("", include("directory.urls")),
]
