"""Read-only validation of the manual Compose example's environment isolation."""

import json
import sys
from pathlib import Path


def validate(document, environment):
    if environment not in {"staging", "production"} or document["name"] != f"namelist-{environment}":
        raise ValueError("Wrong deployment project")
    services = document["services"]
    if services["web"]["environment"]["DJANGO_DEBUG"] != "0":
        raise ValueError("Debug is not allowed in a deployed environment")
    for name in ("db", "maintenance"):
        values = services[name]["environment"]
        if values["BACKUP_ENABLED"] != "1" or values["BACKUP_REPO_TYPE"] != "s3":
            raise ValueError("Deployment requires independent S3 backups")
        if values["BACKUP_REPO_PATH"] != f"/namelist/{environment}":
            raise ValueError("Backup prefix belongs to another environment")
    expected = Path(f"/srv/namelist/{environment}/secrets")
    for secret in document["secrets"].values():
        if Path(secret["file"]).resolve().parent != expected:
            raise ValueError("Server secrets must be isolated in this environment's protected directory")
    for name, service in services.items():
        if name != "web" and service.get("ports"):
            raise ValueError("Private data/worker ports must not be published")
    ports = services["web"].get("ports", [])
    if len(ports) != 1:
        raise ValueError("Web requires exactly one loopback HTTP port")
    port = ports[0]
    if (
        port.get("host_ip") != "127.0.0.1"
        or port.get("target") != 8000
        or port.get("protocol", "tcp") != "tcp"
        or not str(port.get("published", "")).isdigit()
        or not 1 <= int(port["published"]) <= 65535
        or any(service.get("network_mode") == "host" for service in services.values())
    ):
        raise ValueError("Web must publish HTTP only on a loopback port targeting 8000")


if __name__ == "__main__":
    try:
        validate(json.load(sys.stdin), sys.argv[1])
    except (KeyError, ValueError, IndexError) as error:
        sys.exit(str(error))
