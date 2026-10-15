from allauth.socialaccount.signals import social_account_added, social_account_removed, social_account_updated
from django.dispatch import receiver

from .services import connected


@receiver(social_account_added, dispatch_uid="providers.bind_added")
def account_added(sender, request, sociallogin, **kwargs):
    connected(request, sociallogin, newly_added=True)


@receiver(social_account_updated, dispatch_uid="providers.bind_updated")
def account_updated(sender, request, sociallogin, **kwargs):
    connected(request, sociallogin)


@receiver(social_account_removed, dispatch_uid="providers.bind_removed")
def account_removed(sender, request, socialaccount, **kwargs):
    # Also cover allauth's built-in disconnect form, not just /platforms/.
    from core.services import disconnect_platform
    disconnect_platform(socialaccount.user, socialaccount.provider)

