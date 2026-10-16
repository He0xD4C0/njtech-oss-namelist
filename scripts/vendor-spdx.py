"""Prepare pinned official SPDX data at build time; audits never use the network."""

import argparse
import hashlib
import io
import json
import tarfile
import urllib.request
from pathlib import Path

VERSION = "3.29.0"
COMMIT = "31ba1a50e5397e00a304dbadc76531740e89ee48"
ARCHIVE_SHA256 = "5fe8c93bbceaac6d2a3df6cf9fe07abd9af2eca3ec37cf08e4aa74bb24a98683"
URL = f"https://codeload.github.com/spdx/license-list-data/tar.gz/{COMMIT}"
TOOLS_VERSION = "2.0.7"
TOOLS_SHA256 = "bf99a8b2ffbea238c1451c6b4be568870f18b4aac93d0dbff755f2b0b7ce43f3"
TOOLS_URL = (f"https://repo.maven.apache.org/maven2/org/spdx/tools-java/{TOOLS_VERSION}/"
             f"tools-java-{TOOLS_VERSION}-jar-with-dependencies.jar")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--archive", type=Path, help="Use a previously downloaded, checksum-verified archive")
    parser.add_argument("--tools-jar", type=Path, help="Use a previously downloaded official tools JAR")
    args = parser.parse_args()
    if args.archive:
        payload = args.archive.read_bytes()
    else:
        with urllib.request.urlopen(URL, timeout=120) as response:
            payload = response.read()
    if hashlib.sha256(payload).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("SPDX source archive checksum mismatch")
    if args.tools_jar:
        tools = args.tools_jar.read_bytes()
    else:
        with urllib.request.urlopen(TOOLS_URL, timeout=120) as response:
            tools = response.read()
    if hashlib.sha256(tools).hexdigest() != TOOLS_SHA256:
        raise ValueError("SPDX tools JAR checksum mismatch")
    licenses = {}
    prefix = f"license-list-data-{COMMIT}/json/details/"
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive:
            if member.isfile() and member.name.startswith(prefix) and member.name.endswith(".json"):
                data = json.load(archive.extractfile(member))
                licenses[data["licenseId"]] = {
                    "template": data["standardLicenseTemplate"], "text": data["licenseText"],
                }
    if not licenses:
        raise ValueError("SPDX source archive contains no templates")
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    args.destination.with_name("tools-java.jar").write_bytes(tools)
    args.destination.write_text(json.dumps({
        "version": VERSION, "commit": COMMIT, "licenses": licenses,
    }, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    print(f"Prepared {len(licenses)} SPDX {VERSION} templates")


if __name__ == "__main__":
    main()
