#!/usr/bin/env python3
"""Render pgBackRest's private runtime config from independent server secret files."""

import os
import sys
import tempfile
from pathlib import Path

# PostgreSQL's system Python also runs this script without the application runtime.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from deploy.backup.readiness import configuration  # noqa: E402


def main():
    output = Path("/etc/pgbackrest/pgbackrest.conf")
    from deploy.backup import managed
    content = managed.config_path(managed.active()["revision"]).read_text() if managed.enabled() else configuration()
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
