"""Execution ownership shared by policy sync tasks and their recovery endpoints."""

import math
import subprocess
import threading
import uuid
from contextlib import contextmanager
from datetime import timedelta

from celery.exceptions import Retry
from django.db import close_old_connections, connections, transaction
from django.utils import timezone

from .models import SourceSyncState, SyncRun
from .runtime import get_setting

LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 30
SOURCE_KEYS = {"documents": "policy_source_url", "licenses": "license_source_url"}


def recovery_state(run):
    if run.status in {"failed", "queue-failed"}:
        return "recoverable"
    if run.status != "running":
        return ""
    remaining = (run.lease_expires_at - timezone.now()).total_seconds() if run.lease_expires_at else 0
    if not run.execution_token or remaining <= 0:
        return "recoverable"
    return "waiting" if remaining <= LEASE_SECONDS - 2 * HEARTBEAT_SECONDS else "executing"


def queue_sync(kind, recover_id=None):
    from .tasks import sync_documents, sync_license_policy

    key = SOURCE_KEYS[kind]
    with transaction.atomic():
        state, _ = SourceSyncState.objects.get_or_create(kind=kind)
        state = SourceSyncState.objects.select_for_update().get(pk=state.pk)
        source = get_setting(key)
        if not source:
            raise ValueError("No sync source")
        latest = SyncRun.objects.filter(kind=kind).order_by("-created_at", "-pk").first()
        if recover_id is not None:
            run = SyncRun.objects.select_for_update().filter(pk=recover_id, kind=kind).first()
            if not run:
                raise ValueError("Unknown sync run")
            if not latest or latest.pk != run.pk or recovery_state(run) != "recoverable":
                return run, False
            # Reuse this request and generation; a fresh execution token fences the old worker.
            run.status, run.execution_token, run.lease_expires_at = "queued", None, None
            run.save(update_fields=["status", "execution_token", "lease_expires_at"])
        else:
            if latest and latest.status in {"queued", "running", "retrying"}:
                return latest, False
            state.generation += 1
            state.save(update_fields=["generation"])
            run = SyncRun.objects.create(kind=kind, status="queued")
        try:
            # The consumer claims under the same row locks, after this short transaction commits.
            (sync_documents if kind == "documents" else sync_license_policy).delay(
                run_id=run.pk, source=source, generation=state.generation,
            )
        except Exception:
            run.status = "queue-failed"
            run.save(update_fields=["status"])
            return run, False
        return run, True


class OwnershipLost(Exception):
    pass


class SyncLease:
    def __init__(self, run, source, generation):
        self.run, self.source, self.generation = run, source, generation
        self.stopped = threading.Event()
        self.lost = threading.Event()

    def renew(self):
        now = timezone.now()
        updated = SyncRun.objects.filter(
            pk=self.run.pk, status="running", execution_token=self.run.execution_token,
            lease_expires_at__gt=now,
        ).update(lease_expires_at=now + timedelta(seconds=LEASE_SECONDS))
        if not updated:
            self.lost.set()
        return bool(updated)

    def heartbeat(self):
        close_old_connections()
        try:
            while not self.stopped.wait(HEARTBEAT_SECONDS):
                try:
                    if not self.renew():
                        break
                except Exception:
                    self.lost.set()
                    break
        finally:
            connections.close_all()

    @contextmanager
    def maintain(self):
        thread = threading.Thread(target=self.heartbeat, daemon=True, name=f"sync-lease-{self.run.pk}")
        thread.start()
        try:
            yield
        finally:
            self.stopped.set()
            thread.join(timeout=1)

    @contextmanager
    def publish(self):
        with transaction.atomic():
            state = SourceSyncState.objects.select_for_update().get(kind=self.run.kind)
            current = SyncRun.objects.select_for_update().get(pk=self.run.pk)
            if (self.lost.is_set() or current.status != "running"
                    or current.execution_token != self.run.execution_token
                    or not current.lease_expires_at or current.lease_expires_at <= timezone.now()):
                raise OwnershipLost
            if state.generation != self.generation or get_setting(SOURCE_KEYS[self.run.kind]) != self.source:
                # This older request must not change effective content.
                current.status, current.execution_token, current.lease_expires_at = "superseded", None, None
                current.save(update_fields=["status", "execution_token", "lease_expires_at"])
                valid = False
            else:
                valid = True
            if valid:
                yield
        if not valid:
            raise OwnershipLost

    def finish(self, status, sha=""):
        # Called only inside publish(), with both ownership and source rows locked.
        updated = SyncRun.objects.filter(
            pk=self.run.pk, status="running", execution_token=self.run.execution_token,
            lease_expires_at__gt=timezone.now(),
        ).update(status=status, source_sha=sha, message="", execution_token=None, lease_expires_at=None)
        if not updated or self.lost.is_set():
            raise OwnershipLost


def execute_sync(task, kind, work, run_id=None, source=None, generation=None, business_retries=0):
    with transaction.atomic():
        state, _ = SourceSyncState.objects.get_or_create(kind=kind)
        state = SourceSyncState.objects.select_for_update().get(pk=state.pk)
        if run_id is None:
            # Direct calls remain supported; all broker producers supply a durable run ID.
            state.generation += 1
            state.save(update_fields=["generation"])
            generation = state.generation
            run = SyncRun.objects.create(kind=kind, status="queued")
        else:
            run = SyncRun.objects.select_for_update().get(pk=run_id, kind=kind)
        source = get_setting(SOURCE_KEYS[kind]) if source is None else source
        generation = state.generation if generation is None else generation
        if run.status not in {"queued", "retrying", "running"}:
            return run.status
        if source != get_setting(SOURCE_KEYS[kind]) or generation != state.generation or not source:
            run.status, run.execution_token, run.lease_expires_at = "superseded" if source else "not-configured", None, None
            run.save(update_fields=["status", "execution_token", "lease_expires_at"])
            return run.status
        if run.status == "running" and recovery_state(run) != "recoverable":
            wait = max(1, math.ceil((run.lease_expires_at - timezone.now()).total_seconds()) + 1)
        else:
            wait = 0
            run.status = "running"
            run.execution_token = uuid.uuid4()
            run.lease_expires_at = timezone.now() + timedelta(seconds=LEASE_SECONDS)
            run.save(update_fields=["status", "execution_token", "lease_expires_at"])
    kwargs = {"run_id": run.pk, "source": source, "generation": generation, "business_retries": business_retries}
    if wait:
        if not task.request.called_directly and (task.request.delivery_info or {}).get("redelivered"):
            raise task.retry(countdown=wait, kwargs=kwargs, max_retries=None)
        return "running"
    lease = SyncLease(run, source, generation)
    with lease.maintain():
        try:
            return work(lease, source, generation)
        except OwnershipLost:
            return "superseded" if SyncRun.objects.filter(pk=run.pk, status="superseded").exists() else "ownership-lost"
        except Exception as exc:
            retry = isinstance(exc, (ConnectionError, subprocess.SubprocessError)) and not task.request.called_directly and business_retries < 4
            try:
                with lease.publish():
                    lease.finish("retrying" if retry else "failed")
            except OwnershipLost:
                return "ownership-lost"
            if retry:
                kwargs["business_retries"] += 1
                try:
                    raise task.retry(exc=exc, countdown=min(60, 2 ** business_retries), kwargs=kwargs, max_retries=None)
                except Retry:
                    raise
                except Exception:
                    SyncRun.objects.filter(pk=run.pk, status="retrying", execution_token__isnull=True).update(status="queue-failed")
                    raise
            from .tasks import notify_failure
            try:
                notify_failure.delay(kind)
            except Exception:
                pass
            raise
