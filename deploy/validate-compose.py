"""Read-only validation of the manual Compose example's environment isolation."""

import json
import sys
from ipaddress import ip_address, ip_network
from pathlib import Path


def validate(document, environment):
    if environment not in {"staging", "production"} or document["name"] != f"namelist-{environment}":
        raise ValueError("Wrong deployment project")
    services = document["services"]
    if services["web"]["environment"]["DJANGO_DEBUG"] != "0":
        raise ValueError("Debug is not allowed in a deployed environment")
    for service in services.values():
        values = service.get("environment", {})
        networks = [ip_network(value.strip()) for value in values.get("TRUSTED_PROXY_CIDRS", "").split(",") if value.strip()]
        if values.get("TRUST_PROXY_HEADERS") == "1" and not networks:
            raise ValueError("TRUST_PROXY_HEADERS requires explicit TRUSTED_PROXY_CIDRS")
        mode = values.get("BACKUP_CONFIG_MODE", "server")
        if mode not in {"server", "application"}:
            raise ValueError("Invalid backup configuration mode")
        if values.get("PRIVATE_HTTP", "0") not in {"0", "1"}:
            raise ValueError("PRIVATE_HTTP must be 0 or 1")
        if values.get("PRIVATE_HTTP") == "1":
            import ipaddress
            from urllib.parse import urlsplit
            origin = urlsplit(values.get("SITE_BASE_URL", ""))
            address = ipaddress.ip_address(origin.hostname or "")
            if origin.scheme != "http" or not any(address in ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
                raise ValueError("PRIVATE_HTTP requires an RFC1918 HTTP site origin")
        if values.get("STAGING_HTTP", "0") not in {"0", "1"}:
            raise ValueError("STAGING_HTTP must be 0 or 1")
        if values.get("STAGING_HTTP") == "1" and (
            environment != "staging" or values.get("SITE_ENVIRONMENT") != "staging"
        ):
            raise ValueError("STAGING_HTTP is only allowed in the staging environment")
    for name in ("db", "maintenance"):
        values = services[name]["environment"]
        if values["BACKUP_ENABLED"] not in {"0", "1"} or values["BACKUP_REPO_TYPE"] != "s3":
            raise ValueError("Deployment requires independent S3 backups")
        if values.get("SITE_ENVIRONMENT") != environment:
            raise ValueError("Backup environment belongs to another environment")
        if environment == "production" and values.get("BACKUP_CONFIG_MODE", "server") == "server" and (
            values["BACKUP_ENABLED"] != "1"
            or not all(values.get(key) for key in ("BACKUP_S3_ENDPOINT", "BACKUP_S3_BUCKET", "BACKUP_S3_REGION"))
        ):
            raise ValueError("Production requires configured independent S3 backups")
        if values["BACKUP_REPO_PATH"] != f"/namelist/{environment}":
            raise ValueError("Backup prefix belongs to another environment")
    modes = {services[name].get("environment", {}).get("BACKUP_CONFIG_MODE", "server") for name in ("db", "maintenance", "web", "worker", "scanner", "beat", "initialize")}
    if len(modes) != 1:
        raise ValueError("Backup configuration mode must match across services")
    if modes == {"application"}:
        for name, service in services.items():
            mounts = [m for m in service.get("volumes", []) if m["target"] == "/var/lib/namelist-backup"]
            if name in {"db", "maintenance"}:
                if len(mounts) != 1 or mounts[0].get("source") != "backup_config" or mounts[0].get("type") != "volume" or bool(mounts[0].get("read_only")) != (name == "db"):
                    raise ValueError("Managed backup volume must be writable only by maintenance and read-only in db")
            elif mounts:
                raise ValueError("Managed backup configuration must not be exposed to application services")
    expected = Path(f"/srv/namelist/{environment}/secrets")
    for name, service in services.items():
        mounts = [mount for mount in service.get("volumes", []) if mount["target"] == "/run/backup-secrets"]
        if name in {"db", "maintenance"}:
            if len(mounts) != 1:
                raise ValueError("Backup credentials require an isolated read-only directory")
            mount = mounts[0]
            if (
                mount["type"] != "bind" or Path(mount["source"]).resolve() != expected / "backup"
                # Compose v2 omits false values from its rendered JSON.
                or not mount.get("read_only") or mount.get("bind", {}).get("create_host_path", False) is not False
                or mount.get("bind", {}).get("selinux") != "z"
            ):
                raise ValueError("Backup credentials require an isolated read-only directory")
        elif mounts:
            raise ValueError("Backup credentials belong only to database and maintenance")
    for secret in document["secrets"].values():
        if Path(secret["file"]).resolve().parent != expected:
            raise ValueError("Server secrets must be isolated in this environment's protected directory")
    for name, service in services.items():
        if name != "web" and service.get("ports"):
            raise ValueError("Private data/worker ports must not be published")
    ports = services["web"].get("ports", [])
    if len(ports) != 1:
        raise ValueError("Web requires exactly one HTTP port")
    port = ports[0]
    try:
        address = ip_address(port.get("host_ip") or "")
        allowed_bind = address.is_loopback or any(
            address in ip_network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        )
    except ValueError:
        allowed_bind = False
    if (
        not allowed_bind
        or port.get("target") != 8000
        or port.get("protocol", "tcp") != "tcp"
        or not str(port.get("published", "")).isdigit()
        or not 1 <= int(port["published"]) <= 65535
        or any(service.get("network_mode") == "host" for service in services.values())
    ):
        raise ValueError("Web must publish HTTP only on a loopback or RFC1918 address targeting 8000")


if __name__ == "__main__":
    try:
        validate(json.load(sys.stdin), sys.argv[1])
    except (KeyError, ValueError, IndexError) as error:
        sys.exit(str(error))
