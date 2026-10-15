from allauth.account.adapter import DefaultAccountAdapter

from .models import PolicyConsent, username_validator
from .runtime import get_setting, registration_ready


class AccountAdapter(DefaultAccountAdapter):
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
