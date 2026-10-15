"""Record immutable CI images and package the manual Compose example; never deploy."""

import argparse
import io
import json
import re
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {
    "web": "APP_IMAGE",
    "scanner": "SCANNER_IMAGE",
    "maintenance": "MAINTENANCE_IMAGE",
    "postgres": "POSTGRES_BACKUP_IMAGE",
}
IMAGE = re.compile(r"ghcr\.io/[a-z0-9_./-]+@sha256:[0-9a-f]{64}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
RUN_ID = re.compile(r"[1-9][0-9]*\Z")
# Files, not directories: untracked credentials, caches and scripts cannot enter the bundle.
BUNDLE_FILES = (
    "compose.yaml",
    ".env.example",
    "README.md",
    "LICENSE",
    "docs/operations.md",
    "docs/release-acceptance.md",
    "docs/runtime-contract.md",
    "docs/implementation-status.md",
    "licensecheck/README.md",
    "deploy/validate-compose.py",
    "deploy/backup/backup.sh",
    "deploy/backup/maintenance.sh",
    "deploy/backup/render-config.py",
    "deploy/backup/readiness.py",
    "deploy/backup/restore-drill.sh",
    "deploy/backup/restore.py",
    "deploy/backup/scheduler.py",
    "deploy/postgres/entrypoint.sh",
    "deploy/postgres/init-app-role.sh",
    "deploy/postgres/init-backup-stanza.sh",
)


def identity(sha, tag, run_id, attempt):
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ValueError("Invalid source commit")
    if not isinstance(tag, str) or not tag.startswith("dev-") or "/" in tag:
        raise ValueError("Only dev-* source tags are accepted")
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError("Invalid CI run ID")
    if type(attempt) is not int or attempt < 1:
        raise ValueError("Invalid CI run attempt")
    return {"source_sha": sha, "source_tag": tag, "ci_run_id": run_id, "ci_run_attempt": attempt}


def validate(document):
    if not isinstance(document, dict):
        raise ValueError("Manifest must be an object")
    identity(
        document.get("source_sha"), document.get("source_tag"),
        document.get("ci_run_id"), document.get("ci_run_attempt"),
    )
    images = document.get("images", {})
    if not isinstance(images, dict) or set(images) != set(TARGETS):
        raise ValueError("All four image digests are required")
    for reference in images.values():
        if not isinstance(reference, str) or not IMAGE.fullmatch(reference):
            raise ValueError("Only immutable GHCR digest references are accepted")
    return document


def environment(document):
    validate(document)
    return "".join(f"{TARGETS[target]}={document['images'][target]}\n" for target in TARGETS)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    image = commands.add_parser("image")
    for field in ("target", "reference", "sha", "tag", "run_id"):
        image.add_argument(field)
    image.add_argument("attempt", type=int)
    image.add_argument("output")
    bundle = commands.add_parser("bundle")
    for field in ("directory", "output", "sha", "tag", "run_id"):
        bundle.add_argument(field)
    bundle.add_argument("attempt", type=int)
    env = commands.add_parser("env")
    env.add_argument("manifest")
    env.add_argument("output")
    args = parser.parse_args(argv)
    if args.command == "env":
        Path(args.output).write_text(environment(json.loads(Path(args.manifest).read_text())))
        return
    metadata = identity(args.sha, args.tag, args.run_id, args.attempt)
    if args.command == "image":
        if args.target not in TARGETS or not IMAGE.fullmatch(args.reference):
            raise ValueError("Invalid image record")
        Path(args.output).write_text(
            json.dumps(metadata | {"target": args.target, "reference": args.reference}, indent=2) + "\n"
        )
        return
    images = {}
    for source in sorted(Path(args.directory).glob("*/image.json")):
        record = json.loads(source.read_text())
        if not isinstance(record, dict):
            raise ValueError("Image record must be an object")
        identity(
            record.get("source_sha"), record.get("source_tag"),
            record.get("ci_run_id"), record.get("ci_run_attempt"),
        )
        if any(record.get(key) != value for key, value in metadata.items()):
            raise ValueError("Mismatched build identity")
        target = record.get("target")
        if not isinstance(target, str) or target not in TARGETS or target in images:
            raise ValueError("Unknown or duplicate image target")
        images[target] = record.get("reference")
    document = validate(metadata | {"images": images})
    with tarfile.open(args.output, "w:gz") as archive:
        for filename in BUNDLE_FILES:
            source = ROOT / filename
            if not source.is_file() or source.is_symlink() or not source.resolve().is_relative_to(ROOT):
                raise ValueError(f"Bundle input must be a regular repository file: {filename}")
            archive.add(source, arcname=filename, recursive=False)
        for filename, value in (
            ("manifest.json", json.dumps(document, indent=2) + "\n"),
            ("images.env", environment(document)),
        ):
            data = value.encode()
            entry = tarfile.TarInfo(filename)
            entry.size = len(data)
            entry.mode = 0o644
            archive.addfile(entry, io.BytesIO(data))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        sys.exit(str(error))
