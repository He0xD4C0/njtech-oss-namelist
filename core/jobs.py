"""Renew and fence long-running audit and approval executions."""

from contextlib import contextmanager
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .sync import OwnershipLost, SyncLease


class JobLease(SyncLease):
    seconds = 180

    def __init__(self, job):
        super().__init__(job, None, None)
        self.model = type(job)

    def owned(self):
        return self.model.objects.filter(
            pk=self.run.pk, status="running", execution_token=self.run.execution_token,
            lease_expires_at__gt=timezone.now(),
        )

    def renew(self):
        if not self.owned().update(lease_expires_at=timezone.now() + timedelta(seconds=self.seconds)):
            self.lost.set()
        return not self.lost.is_set()

    @contextmanager
    def publish(self):
        with transaction.atomic():
            current = self.owned().select_for_update().first()
            if not current or self.lost.is_set():
                raise OwnershipLost
            yield current

    def finish(self, **values):
        with self.publish() as current:
            for key, value in values.items():
                setattr(current, key, value)
            current.execution_token = current.lease_expires_at = None
            current.save()
