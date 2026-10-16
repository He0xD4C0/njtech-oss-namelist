from allauth.account.auth_backends import AuthenticationBackend as BaseBackend
from allauth.account.models import EmailAddress
from allauth.account.stages import EmailVerificationStage
from allauth.account.views import SignupView as BaseSignupView
from allauth.socialaccount.views import SignupView as BaseSocialSignupView
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.shortcuts import redirect

from .i18n import text


class AuthenticationBackend(BaseBackend):
    def _authenticate_by_email(self, email, password):
        address = EmailAddress.objects.filter(email__iexact=email, verified=True).select_related("user").first()
        return self._check_password(address.user if address else None, password)


class VerifiedMailboxStage(EmailVerificationStage):
    def is_resumable(self, request):
        return True

    def handle(self):
        user = self.login.user
        if user and (user.is_superuser or EmailAddress.objects.filter(user=user, verified=True).exists()):
            return None, True
        # Legacy users authenticate by username/password only to this restricted
        # stage. Never call allauth's sync_email_address (it inserts pending mail).
        return redirect("account_email_verification_sent"), True


class SignupConflictMixin:
    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except (IntegrityError, ValidationError) as exc:
            form.add_error(None, " ".join(exc.messages) if isinstance(exc, ValidationError) else text(
                "用户名或邮箱已被使用，请刷新后重试。", "The username or email is already in use. Refresh and try again."))
            return self.form_invalid(form)


class SignupView(SignupConflictMixin, BaseSignupView):
    pass


class SocialSignupView(SignupConflictMixin, BaseSocialSignupView):
    pass
