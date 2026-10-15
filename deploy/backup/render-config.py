#!/usr/bin/env python3
"""Render pgBackRest's private runtime config from independent server secret files."""

import os
import re
import tempfile
from pathlib import Path


def setting(name, default="", required=False):
    value = os.environ.get(name, default)
    if (required and not value) or any(c in value for c in "\r\n\x00"):
        raise ValueError(f"Invalid or missing {name}")
    return value


def secret(name):
    value = Path(setting(name, required=True)).read_text().strip()
    if not value or any(c in value for c in "\r\n\x00"):
        raise ValueError(f"Invalid secret file for {name}")
    return value


def configuration():
    stanza = setting("BACKUP_STANZA", "namelist")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,40}", stanza):
        raise ValueError("Invalid BACKUP_STANZA")
    kind = setting("BACKUP_REPO_TYPE", "s3")
    if kind not in {"s3", "posix"}:
        raise ValueError("Only s3 or explicit local-test posix repositories are supported")
    repo_path = setting("BACKUP_REPO_PATH", required=True)
    if not repo_path.startswith("/") or repo_path == "/" or ".." in Path(repo_path).parts:
        raise ValueError("BACKUP_REPO_PATH must be an environment-specific absolute prefix")
    if kind == "posix" and not repo_path.startswith("/var/lib/pgbackrest/"):
        raise ValueError("Local backups must stay under /var/lib/pgbackrest/")
    password = secret("BACKUP_CIPHER_FILE")
    if len(password) < 32:
        raise ValueError("Backup cipher passphrase must have at least 32 characters")
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
        # No archive queue cap: pgBackRest must never discard WAL to meet a space limit.
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
        for name, option in [
            ("BACKUP_S3_ENDPOINT", "repo1-s3-endpoint"),
            ("BACKUP_S3_BUCKET", "repo1-s3-bucket"),
            ("BACKUP_S3_REGION", "repo1-s3-region"),
        ]:
            value = setting(name, required=True)
            if name == "BACKUP_S3_ENDPOINT" and ("://" in value or "/" in value):
                raise ValueError("BACKUP_S3_ENDPOINT is a hostname, not a URL")
            lines.append(f"{option}={value}")
        style = setting("BACKUP_S3_URI_STYLE", "path")
        if style not in {"path", "host"}:
            raise ValueError("Invalid BACKUP_S3_URI_STYLE")
        lines += [
            f"repo1-s3-uri-style={style}",
            f"repo1-s3-key={secret('BACKUP_S3_KEY_FILE')}",
            f"repo1-s3-key-secret={secret('BACKUP_S3_SECRET_FILE')}",
            "repo1-storage-verify-tls=y",
        ]
    lines += [
        "",
        f"[{stanza}]",
        f"pg1-path={setting('BACKUP_PG_PATH', '/var/lib/postgresql/data')}",
        f"pg1-socket-path={setting('BACKUP_PG_SOCKET', '/var/run/postgresql')}",
        f"pg1-user={setting('BACKUP_PG_USER', 'postgres')}",
        "pg1-database=postgres",
        "",
    ]
    return "\n".join(lines)


def main():
    output = Path("/etc/pgbackrest/pgbackrest.conf")
    content = configuration()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".config-", dir=output.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        # Configuration values and secret contents must not appear in diagnostics.
        raise SystemExit(
            f"pgBackRest configuration failed ({type(error).__name__}); verify server secret files"
        )
