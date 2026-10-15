from django.db.models import Q
from django.utils import timezone

from .models import Announcement, AnnouncementReceipt


def pending_announcements(user):
    if not user.is_authenticated or not user.is_active:
        return []
    candidates = Announcement.objects.filter(Q(expires_at=None) | Q(expires_at__gt=timezone.now())).filter(
        Q(include_future_users=True) | Q(created_at__gte=user.date_joined)
    )
    receipts = {receipt.announcement_id: receipt for receipt in AnnouncementReceipt.objects.filter(user=user)}
    pending = []
    for item in candidates:
        receipt = receipts.get(item.pk)
        if item.kind == Announcement.Kind.POLICY:
            needed = receipt is None or receipt.confirmed_revision != item.revision
        else:
            needed = receipt is None or receipt.delivered_at is None
        if needed:
            pending.append(item)
    return sorted(pending, key=lambda item: (item.kind != Announcement.Kind.POLICY, item.created_at, item.pk))
