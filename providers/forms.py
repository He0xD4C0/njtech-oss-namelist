from allauth.socialaccount.models import SocialAccount
from django import forms

from .models import PLATFORMS


class DisconnectForm(forms.Form):
    platform = forms.ChoiceField(choices=PLATFORMS)
    confirm = forms.BooleanField(label="确认解绑并撤下该平台全部收录内容 / Disconnect and withdraw all entries on this platform")


class ConnectionsDisconnectForm(forms.Form):
    """Compatibility for an already-open allauth connections form."""
    account = forms.ModelChoiceField(queryset=SocialAccount.objects.none())

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = SocialAccount.objects.filter(user=user, provider__in=[p for p, _ in PLATFORMS])

