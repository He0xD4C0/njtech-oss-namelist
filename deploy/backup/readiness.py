"""Local-only backup configuration and readiness, shared by Python runtimes."""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class NotConfigured(ValueError):
    pass


def setting(name, default=""):
    value = os.environ.get(name, default)
    if any(c in value for c in "\r\n\x00"):
        raise ValueError(f"Invalid {name}")
    return value


def secret(name):
    filename = setting(name)
    if not filename:
        return ""
    try:
        value = Path(filename).read_text().strip()
    except FileNotFoundError:
        return ""
    if any(c in value for c in "\r\n\x00"):
        raise ValueError(f"Invalid secret file for {name}")
    return value


def configuration(values=None, secrets=None):
    read_setting = setting if values is None else lambda name, default="": values.get(name, default)
    read_secret = secret if secrets is None else lambda name: secrets.get(name, "")
    # Validate every supplied value before classifying missing configuration.
    stanza = read_setting("BACKUP_STANZA", "namelist")
    kind = read_setting("BACKUP_REPO_TYPE", "s3")
    repo_path = read_setting("BACKUP_REPO_PATH")
    endpoint = read_setting("BACKUP_S3_ENDPOINT")
    bucket = read_setting("BACKUP_S3_BUCKET")
    region = read_setting("BACKUP_S3_REGION")
    style = read_setting("BACKUP_S3_URI_STYLE", "path")
    pg_path = read_setting("BACKUP_PG_PATH", "/var/lib/postgresql/data")
    pg_socket = read_setting("BACKUP_PG_SOCKET", "/var/run/postgresql")
    pg_user = read_setting("BACKUP_PG_USER", "postgres")
    password = read_secret("BACKUP_CIPHER_FILE")
    key = read_secret("BACKUP_S3_KEY_FILE") if kind == "s3" else ""
    key_secret = read_secret("BACKUP_S3_SECRET_FILE") if kind == "s3" else ""
    if stanza and not re.fullmatch(r"[a-z][a-z0-9_-]{0,40}", stanza):
        raise ValueError("Invalid BACKUP_STANZA")
    if kind and kind not in {"s3", "posix"}:
        raise ValueError("Only s3 or explicit local-test posix repositories are supported")
    if repo_path and (not repo_path.startswith("/") or repo_path == "/" or ".." in Path(repo_path).parts):
        raise ValueError("BACKUP_REPO_PATH must be an environment-specific absolute prefix")
    if kind == "posix" and repo_path and not repo_path.startswith("/var/lib/pgbackrest/"):
        raise ValueError("Local backups must stay under /var/lib/pgbackrest/")
    if password and len(password) < 32:
        raise ValueError("Backup cipher passphrase must have at least 32 characters")
    if endpoint and ("://" in endpoint or "/" in endpoint):
        raise ValueError("BACKUP_S3_ENDPOINT is a hostname, not a URL")
    if style and style not in {"path", "host"}:
        raise ValueError("Invalid BACKUP_S3_URI_STYLE")
    for value in (stanza, kind, repo_path, endpoint, bucket, region, style, password, key, key_secret):
        if any(c in value for c in "\r\n\x00"):
            raise ValueError("Invalid backup configuration value")
    required = [stanza, kind, style, repo_path, password, pg_path, pg_socket, pg_user]
    if kind == "s3":
        required += [endpoint, bucket, region, key, key_secret]
    if not all(required):
        raise NotConfigured("Backup configuration is incomplete")
    lines = [
        "[global]",
        f"repo1-type={kind}",
        f"repo1-path={repo_path}",
        "repo1-cipher-type=aes-256-cbc",
        f"repo1-cipher-pass={password}",
        "repo1-retention-full-type=time",
        "repo1-retention-full=30",
        "repo1-retention-history=30",
        "process-max=2",
        "start-fast=y",
        "compress-type=zst",
        "archive-async=n",
        # Never discard WAL to meet an archive queue limit.
        "archive-timeout=300",
        "io-timeout=120",
        "db-timeout=1800",
        "protocol-timeout=1830",
        "log-level-console=warn",
        "log-level-file=info",
        "log-path=/var/log/pgbackrest",
        "lock-path=/var/spool/pgbackrest/lock",
    ]
    if kind == "s3":
        lines += [
            f"repo1-s3-endpoint={endpoint}",
            f"repo1-s3-bucket={bucket}",
            f"repo1-s3-region={region}",
            f"repo1-s3-uri-style={style}",
            f"repo1-s3-key={key}",
            f"repo1-s3-key-secret={key_secret}",
            "repo1-storage-verify-tls=y",
        ]
    lines += [
        "",
        f"[{stanza}]",
        f"pg1-path={pg_path}",
        f"pg1-socket-path={pg_socket}",
        f"pg1-user={pg_user}",
        "pg1-database=postgres",
        "",
    ]
    return "\n".join(lines)


def readiness():
    from deploy.backup import managed

    if managed.enabled():
        return "ready" if managed.active()["state"] == "active" else "not-configured"
    enabled = setting("BACKUP_ENABLED", "1")
    if enabled not in {"0", "1"}:
        raise ValueError("BACKUP_ENABLED must be 0 or 1")
    state = "disabled"
    if enabled == "1":
        try:
            configuration()
        except NotConfigured:
            state = "not-configured"
        else:
            state = "ready"
    if setting("SITE_ENVIRONMENT", "staging") == "production" and state != "ready":
        raise ValueError("Production requires configured independent S3 backups")
    if setting("SITE_ENVIRONMENT", "staging") == "production" and setting("BACKUP_REPO_TYPE", "s3") != "s3":
        raise ValueError("Production requires independent S3 backups")
    return state


if __name__ == "__main__":
    try:
        state = readiness()
    except (ValueError, OSError):
        sys.exit("Backup configuration invalid; inspect server configuration and secret permissions")
    print(state)
    sys.exit(0 if state == "ready" else 2)
