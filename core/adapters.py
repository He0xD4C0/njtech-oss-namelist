from allauth.account.adapter import DefaultAccountAdapter

from .models import PolicyConsent, username_validator
from .runtime import get_setting, registration_ready


class AccountAdapter(DefaultAccountAdapter):
    def get_email_verification_redirect_url(self, email_address):
        from .models import SiteState

        if SiteState.objects.filter(
            pk=1, activated_at__isnull=True, bootstrap_user_id=email_address.user_id
        ).exists():
            return "/setup/?step=mail&confirmed=1"
        return super().get_email_verification_redirect_url(email_address)

    def is_open_for_signup(self, request):
        return registration_ready()

    def clean_username(self, username, shallow=False):
        username_validator(username)
        return super().clean_username(username, shallow=shallow)

    def get_from_email(self):
        return get_setting("smtp_from_email") or super().get_from_email()

    def save_user(self, request, user, form, commit=True):
        user = super().save_user(request, user, form, commit=commit)
        if commit and form.cleaned_data.get("policy_language"):
            PolicyConsent.objects.create(
                user=user,
                language=form.cleaned_data["policy_language"],
                versions=form.cleaned_data["policy_versions"],
            )
        return user
