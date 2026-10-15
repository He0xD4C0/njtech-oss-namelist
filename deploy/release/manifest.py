"""Validate and package the five immutable image references belonging to one tested commit."""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

TARGETS = {
    "web": "APP_IMAGE",
    "scanner": "SCANNER_IMAGE",
    "maintenance": "MAINTENANCE_IMAGE",
    "postgres": "POSTGRES_BACKUP_IMAGE",
    "static": "STATIC_IMAGE",
}
IMAGE = re.compile(r"ghcr\.io/[a-z0-9_./-]+@sha256:[0-9a-f]{64}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")


def validate(document):
    if not SHA.fullmatch(document.get("source_sha", "")):
        raise ValueError("Invalid source commit")
    images = document.get("images", {})
    if set(images) != set(TARGETS):
        raise ValueError("All five image digests are required")
    for reference in images.values():
        if not isinstance(reference, str) or not IMAGE.fullmatch(reference):
            raise ValueError("Only immutable GHCR digest references are accepted")
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    image = commands.add_parser("image")
    for field in ("target", "reference", "sha", "output"):
        image.add_argument(field)
    bundle = commands.add_parser("bundle")
    for field in ("directory", "output", "sha", "run_id"):
        bundle.add_argument(field)
    env = commands.add_parser("env")
    env.add_argument("manifest")
    env.add_argument("output")
    run = commands.add_parser("verify-run")
    run.add_argument("run")
    accept = commands.add_parser("accept")
    for field in ("manifest", "reference", "output"):
        accept.add_argument(field)
    verify = commands.add_parser("verify-acceptance")
    verify.add_argument("manifest")
    verify.add_argument("receipt")
    args = parser.parse_args(argv)
    if args.command == "image":
        if args.target not in TARGETS or not IMAGE.fullmatch(args.reference) or not SHA.fullmatch(args.sha):
            parser.error("Invalid image record")
        Path(args.output).write_text(
            json.dumps({"target": args.target, "reference": args.reference, "source_sha": args.sha})
        )
    elif args.command == "bundle":
        images = {}
        for source in Path(args.directory).glob("*/image.json"):
            record = json.loads(source.read_text())
            if record["source_sha"] != args.sha or record["target"] in images:
                raise ValueError("Mismatched or duplicate build evidence")
            images[record["target"]] = record["reference"]
        document = validate({"source_sha": args.sha, "ci_run_id": args.run_id, "images": images})
        Path(args.output).write_text(json.dumps(document, indent=2))
    elif args.command == "env":
        document = validate(json.loads(Path(args.manifest).read_text()))
        Path(args.output).write_text(
            "".join(f"{TARGETS[target]}={reference}\n" for target, reference in document["images"].items())
        )
    elif args.command == "verify-run":
        record = json.loads(Path(args.run).read_text())
        if not (
            record.get("conclusion") == "success"
            and record.get("head_branch") == "main"
            and record.get("event") == "push"
            and record.get("path") == ".github/workflows/release.yml"
        ):
            raise ValueError("Only a successful main release-and-staging run can be promoted")
    elif args.command == "accept":
        document = validate(json.loads(Path(args.manifest).read_text()))
        document.update(
            acceptance_reference=args.reference, restore_verified_at=datetime.now(timezone.utc).isoformat()
        )
        Path(args.output).write_text(json.dumps(document, indent=2))
    else:
        document = validate(json.loads(Path(args.manifest).read_text()))
        receipt = validate(json.loads(Path(args.receipt).read_text()))
        if (
            document["source_sha"] != receipt["source_sha"]
            or document["images"] != receipt["images"]
            or document.get("ci_run_id") != receipt.get("ci_run_id")
            or not receipt.get("restore_verified_at")
            or not receipt.get("acceptance_reference")
        ):
            raise ValueError("This exact staged release has no matching acceptance/restore receipt")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError) as error:
        sys.exit(str(error))
