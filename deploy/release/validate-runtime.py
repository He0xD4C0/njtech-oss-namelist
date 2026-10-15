"""Fail closed on production/staging miswiring before changing running services."""

import json
import sys
from pathlib import Path


def validate(document, environment):
    if environment not in {"staging", "production"} or document["name"] != f"namelist-{environment}":
        raise ValueError("Wrong deployment project")
    services = document["services"]
    if services["web"]["environment"]["DJANGO_DEBUG"] != "0":
        raise ValueError("Debug is not allowed in a deployed environment")
    for name in ("web", "static"):
        if f"{environment}-{name}" not in services[name]["networks"]["frontend"]["aliases"]:
            raise ValueError("Frontend alias belongs to another environment")
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
    for name in ("db", "redis", "web"):
        if services[name].get("ports"):
            raise ValueError("Private application/data ports must not be published")


if __name__ == "__main__":
    try:
        validate(json.load(sys.stdin), sys.argv[1])
    except (KeyError, ValueError, IndexError) as error:
        sys.exit(str(error))
