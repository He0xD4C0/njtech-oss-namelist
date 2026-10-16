"""Authenticated backup operations. Secrets never travel in queue messages or status JSON."""
import json
import secrets
import subprocess
import threading
import uuid
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections, transaction
from django.utils import timezone

from deploy.backup import managed
from deploy.backup.readiness import configuration as render_configuration

from .crypto import decrypt, encrypt
from .i18n import text
from .models import AuditEvent, BackupConfiguration, SyncRun

FIELDS = ("endpoint", "bucket", "region", "access_key", "secret_key", "uri_style")
SECRETS = {"access_key", "secret_key"}
LOCATION = {"endpoint", "bucket", "region", "uri_style"}
BUSY = {"queued", "running"}


def candidate(record):
    return {k: decrypt(v) if k in SECRETS and v else v for k, v in record.values.items()}


@transaction.atomic
def save_candidate(data, user=None):
    if not managed.enabled():
        raise ValueError(text("备份由服务器配置管理。", "Backups are managed by server configuration."))
    BackupConfiguration.objects.get_or_create(pk=1)
    record = BackupConfiguration.objects.select_for_update().get(pk=1)
    values = candidate(record)
    updated = values | {k: v for k, v in data.items() if k in FIELDS and (k not in SECRETS or v)}
    updated.setdefault("instance", str(uuid.uuid4()))
    if record.location and any(updated.get(k) != record.location.get(k) for k in LOCATION):
        raise ValueError(text("备份启用后只能更新访问凭据；存储位置不能修改。", "Only access credentials can change after backups are enabled."))
    if updated != values:
        record.values = {k: encrypt(v) if k in SECRETS else v for k, v in updated.items()}
        record.revision = uuid.uuid4()
        record.tested_revision = None
        record.save()
        AuditEvent.objects.create(actor=user, action="backup.settings.saved", subject=str(record.revision))
    return record


def queue_operation(action, user=None):
    if action not in {"test", "apply", "backup"} or not managed.enabled():
        raise ValueError(text("备份操作不可用。", "Backup operation unavailable."))
    with transaction.atomic():
        record = BackupConfiguration.objects.select_for_update().get(pk=1)
        if action == "apply" and record.tested_revision != record.revision:
            raise ValueError(text("请先测试当前配置的 S3 连通性。", "Test the current S3 configuration first."))
        if action == "backup" and not record.applied_revision:
            raise ValueError(text("请先启用自动备份。", "Enable backups first."))
        now = timezone.now()
        active = SyncRun.objects.filter(kind__startswith="backup-", status__in=BUSY).order_by("-pk").first()
        if active:
            if active.lease_expires_at and active.lease_expires_at > now:
                return active
            SyncRun.objects.filter(pk=active.pk, status__in=BUSY).update(status="failed", message="interrupted", execution_token=None)
        revision = record.applied_revision if action == "backup" else record.revision
        run = SyncRun.objects.create(kind=f"backup-{action}", status="queued", source_sha=str(revision),
                                     lease_expires_at=now + timedelta(seconds=120))
        try:
            backup_operation.apply_async(args=[run.pk], queue="maintenance")
        except Exception:
            run.status, run.message = "queue-failed", "queue-unavailable"
            run.save(update_fields=["status", "message"])
        AuditEvent.objects.create(actor=user, action=f"backup.{action}.requested", subject=str(run.pk))
        return run


def public_status():
    record = BackupConfiguration.objects.filter(pk=1).first()
    report = cache.get("backup-readiness") or {}
    run = SyncRun.objects.filter(kind__startswith="backup-").order_by("-pk").first()
    live = bool(report.get("checked_at"))
    busy = bool(run and run.status in BUSY and run.lease_expires_at and run.lease_expires_at > timezone.now())
    operation_busy = busy
    busy = busy or bool(report.get("backup", {}).get("busy"))
    state = "not-configured"
    if record and record.values:
        state = "saved"
        if record.tested_revision == record.revision:
            state = "tested"
        if record.applied_revision:
            state = "enabled"
    if run and run.status in {"failed", "queue-failed"}:
        state = "failed"
    if run and run.status in BUSY:
        state = run.kind.removeprefix("backup-") + "ing" if busy else "interrupted"
    if report.get("backup", {}).get("last_success") and state == "enabled":
        state = "success"
    if not operation_busy and report.get("backup", {}).get("state") == "running" and busy:
        state = "backuping"
    if not operation_busy and report.get("backup", {}).get("state") in {"failed", "interrupted"} and state in {"enabled", "success"}:
        state = report["backup"]["state"]
    if not live and managed.enabled():
        state = "unavailable"
    labels = {
        "not-configured": ("尚无数据库备份保护，可稍后配置。", "No database backup protection. You can configure it later."),
        "saved": ("配置已保存，尚未测试。", "Settings saved; not tested."),
        "tested": ("S3 连通性测试通过，尚不代表已有备份。", "S3 test passed; this does not mean a backup exists."),
        "enabled": ("自动备份已启用，等待首次备份成功。", "Backups enabled; awaiting the first successful backup."),
        "testing": ("正在测试 S3 写入、读取、列举和删除权限。", "Testing S3 write, read, list and delete access."),
        "applying": ("正在应用配置并执行首次全量备份。", "Applying configuration and creating a full backup."),
        "backuping": ("正在执行全量备份。", "Creating a full backup."),
        "success": ("已有成功的数据库备份。", "A successful database backup is available."),
        "failed": ("操作失败；已保存的配置和已有备份会保留，请检查下方原因后重试。", "Operation failed. Saved settings and existing backups are retained; check the reason and retry."),
        "interrupted": ("操作已中断，可重新尝试。", "Operation interrupted; you can retry."),
        "unavailable": ("维护服务暂不可用，尚无法确认备份状态。", "Maintenance service unavailable; backup status cannot be confirmed."),
    }
    errors = {
        "access-denied": ("访问被拒绝，请核对密钥和桶权限。", "Access denied. Check credentials and bucket permissions."),
        "timeout": ("存储服务连接超时，请检查地址和网络后重试。", "Storage timed out. Check the endpoint and network."),
        "connection": ("无法连接存储服务，请核对地址、TLS 证书和网络。", "Cannot connect. Check endpoint, TLS certificate and network."),
        "storage-test": ("存储权限或数据回读验证失败，请检查桶权限。", "Storage permission or readback test failed. Check bucket permissions."),
        "backup-failed": ("备份或 WAL 归档验证失败，请检查维护服务与存储权限。", "Backup or WAL archive verification failed. Check maintenance and storage permissions."),
        "queue-unavailable": ("任务未能入队，请检查任务服务后重试。", "Could not queue the operation. Check the task service and retry."),
        "interrupted": ("执行进程已中断，请重试。", "The execution process was interrupted. Retry."),
    }
    error = errors.get((run.message if run else "") or report.get("backup", {}).get("error", ""))
    return {"state": state, "label": text(*labels[state]), "error": text(*error) if error else "",
            "revision": str(record.revision) if record else "", "operation_id": run.pk if run else None,
            "operation_status": run.status if run else "", "busy": busy,
            "tested": bool(record and record.tested_revision == record.revision),
            "enabled": bool(record and record.applied_revision), "managed": managed.enabled(),
            "last_success": report.get("backup", {}).get("last_success", ""),
            "prefix": record.location.get("prefix", "") if record else ""}


def test_storage(values, prefix):
    import boto3
    from botocore.config import Config
    client = boto3.client("s3", endpoint_url="https://" + values["endpoint"], region_name=values["region"],
        aws_access_key_id=values["access_key"], aws_secret_access_key=values["secret_key"],
        config=Config(connect_timeout=10, read_timeout=20, retries={"max_attempts": 1},
                      s3={"addressing_style": "virtual" if values["uri_style"] == "host" else "path"}))
    key = prefix.strip("/") + "/.connection-test/" + str(uuid.uuid4())
    payload = secrets.token_bytes(32)
    written = False
    try:
        client.put_object(Bucket=values["bucket"], Key=key, Body=payload)
        written = True
        body = client.get_object(Bucket=values["bucket"], Key=key)["Body"]
        try:
            if body.read() != payload:
                raise ValueError("readback")
        finally:
            body.close()
        listed = client.list_objects_v2(Bucket=values["bucket"], Prefix=key)
        if key not in [item["Key"] for item in listed.get("Contents", [])]:
            raise ValueError("listing")
    finally:
        if written:
            client.delete_object(Bucket=values["bucket"], Key=key)


def error_code(error):
    from botocore.exceptions import BotoCoreError, ClientError, ConnectTimeoutError, ReadTimeoutError
    if isinstance(error, ClientError):
        return "access-denied" if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") in {401, 403} else "storage-test"
    if isinstance(error, (ConnectTimeoutError, ReadTimeoutError, subprocess.TimeoutExpired)):
        return "timeout"
    if isinstance(error, BotoCoreError):
        return "connection"
    return "backup-failed" if isinstance(error, subprocess.SubprocessError) else "storage-test"


class LostExecution(Exception):
    pass


class Execution:
    def __init__(self, run, token):
        self.run, self.token = run, token
        self.stop = threading.Event()

    def renew(self):
        while not self.stop.wait(30):
            close_old_connections()
            try:
                changed = SyncRun.objects.filter(pk=self.run.pk, execution_token=self.token,
                    status="running", lease_expires_at__gt=timezone.now()).update(
                        lease_expires_at=timezone.now() + timedelta(seconds=120))
                if not changed:
                    return
            except Exception:
                return
            finally:
                close_old_connections()

    def check(self, config, *, candidate_revision=True):
        current = SyncRun.objects.select_for_update().get(pk=self.run.pk)
        revision = config.revision if candidate_revision else config.applied_revision
        if (current.execution_token != self.token or current.status != "running"
                or current.lease_expires_at <= timezone.now() or str(revision) != self.run.source_sha):
            raise LostExecution()


@shared_task(acks_late=True, reject_on_worker_lost=True)
def backup_operation(run_id):
    from .tasks import report_backup_readiness
    if not managed.enabled():
        return "server-managed"
    token = uuid.uuid4()
    with transaction.atomic():
        config = BackupConfiguration.objects.select_for_update().get(pk=1)
        run = SyncRun.objects.select_for_update().get(pk=run_id)
        if run.status != "queued":
            return run.status
        action = run.kind.removeprefix("backup-")
        revision = config.applied_revision if action == "backup" else config.revision
        if str(revision) != run.source_sha:
            run.status = "superseded"
            run.save(update_fields=["status"])
            return run.status
        run.status, run.execution_token = "running", token
        run.lease_expires_at = timezone.now() + timedelta(seconds=120)
        run.save()
        values = candidate(config)
    execution = Execution(run, token)
    heartbeat = threading.Thread(target=execution.renew, daemon=True)
    heartbeat.start()
    try:
        with managed.lock(blocking=False):
            prefix = f"/namelist/{settings.SITE_ENVIRONMENT}/{values['instance']}"
            if action == "test":
                test_storage(values, prefix)
                with transaction.atomic():
                    config = BackupConfiguration.objects.select_for_update().get(pk=1)
                    execution.check(config)
                    config.tested_revision = config.revision
                    config.save(update_fields=["tested_revision"])
            elif action == "apply":
                active = managed.active(allow_pending=True)
                fixed = {k: values[k] for k in LOCATION} | {"prefix": prefix}
                if active["state"] == "active" and any(active["values"].get(k) != values[k] for k in LOCATION):
                    raise ValueError("immutable location")
                # Persist before stanza creation: a crash must never generate a different cipher on retry.
                repository_file = managed.ROOT / "repository.json"
                try:
                    repository = json.loads(repository_file.read_text())
                except FileNotFoundError:
                    repository = {"cipher": active.get("cipher") or secrets.token_urlsafe(48), "prefix": prefix}
                    managed.write_json(repository_file, repository)
                cipher = repository["cipher"]
                env = {"BACKUP_REPO_TYPE": "s3", "BACKUP_REPO_PATH": prefix,
                    "BACKUP_S3_ENDPOINT": values["endpoint"], "BACKUP_S3_BUCKET": values["bucket"],
                    "BACKUP_S3_REGION": values["region"], "BACKUP_S3_URI_STYLE": values["uri_style"]}
                secret_values = {"BACKUP_CIPHER_FILE": cipher, "BACKUP_S3_KEY_FILE": values["access_key"],
                                 "BACKUP_S3_SECRET_FILE": values["secret_key"]}
                managed.store_config(run.source_sha, render_configuration(env, secret_values))
                managed.command("stanza-create", revision=run.source_sha, timeout=180)
                # Wait for in-flight archiving before opening the short database transaction.
                with managed.lock("archive"), transaction.atomic():
                    config = BackupConfiguration.objects.select_for_update().get(pk=1)
                    execution.check(config)
                    if config.tested_revision != config.revision:
                        raise LostExecution()
                    managed.activate({"state": "active", "revision": run.source_sha, "values": values,
                                      "cipher": cipher, "location": fixed}, archive_locked=True)
                    config.location, config.applied_revision = fixed, config.revision
                    config.save(update_fields=["location", "applied_revision"])
                managed.backup("full")
            elif action == "backup":
                managed.backup("full")
            with transaction.atomic():
                config = BackupConfiguration.objects.select_for_update().get(pk=1)
                execution.check(config, candidate_revision=action != "backup")
                SyncRun.objects.filter(pk=run.pk, execution_token=token).update(
                    status="success", execution_token=None, lease_expires_at=None, message="")
        return "success"
    except LostExecution:
        SyncRun.objects.filter(pk=run.pk, execution_token=token, status="running").update(
            status="superseded", execution_token=None, lease_expires_at=None)
        return "superseded"
    except Exception as error:
        code = "interrupted" if isinstance(error, BlockingIOError) else error_code(error)
        SyncRun.objects.filter(pk=run.pk, execution_token=token, status="running", lease_expires_at__gt=timezone.now()).update(
            status="failed", message=code, execution_token=None, lease_expires_at=None)
        return "failed"
    finally:
        execution.stop.set()
        heartbeat.join(timeout=2)
        report_backup_readiness()
