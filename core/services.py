from django.db import transaction
from django.utils import timezone

from .models import AuditEvent, User


def import_oauth_profile(user, name, avatar_url):
    if not user.full_name and name:
        user.full_name = name[:150]
        user.save(update_fields=["full_name"])
    from .avatar import import_avatar

    import_avatar(user, avatar_url)


@transaction.atomic
def disconnect_platform(user, platform):
    from allauth.socialaccount.models import SocialAccount

    from providers.models import OAuthBinding
    from repositories.models import Submission
    from repositories.services import freeze_completed_months

    freeze_completed_months()
    # Serialize against add/claim transactions and invalidate every queued job.
    User.objects.select_for_update().get(pk=user.pk)
    submissions = (
        Submission.objects.select_for_update()
        .filter(user=user, repository__platform=platform)
        .exclude(status="removed")
    )
    from django.db.models import F

    submissions.update(
        status="removed",
        generation=F("generation") + 1,
        reason="平台解绑 / Platform disconnected",
        changed_at=timezone.now(),
    )
    OAuthBinding.objects.filter(user=user, platform=platform).delete()
    SocialAccount.objects.filter(user=user, provider=platform).delete()
    AuditEvent.objects.create(actor=user, action="platform.disconnected", subject=platform)


def delete_account(user):
    from repositories.services import freeze_completed_months

    freeze_completed_months()
    from .avatar import queue_avatar_deletion

    with transaction.atomic():
        locked = User.objects.select_for_update().get(pk=user.pk)
        queue_avatar_deletion(locked.avatar_key)
        AuditEvent.objects.create(action="account.deleted", subject="deleted-account", detail={})
        # No personal information survives in generic audit subjects/details.
        AuditEvent.objects.filter(actor=locked).update(actor=None, subject="deleted-account", detail={})
        locked.delete()
