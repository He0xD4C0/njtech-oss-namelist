"""Offline SPDX template comparison using the official SPDX Java implementation.

This module does not decide eligibility. Only templates fully matched by a
policy source may be used by the engine to authorize candidate text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from threading import RLock

DATA_VERSION = "3.29.0"
DATA_COMMIT = "31ba1a50e5397e00a304dbadc76531740e89ee48"
TOOLS_VERSION = "2.0.7"
TOOLS_SHA256 = "bf99a8b2ffbea238c1451c6b4be568870f18b4aac93d0dbff755f2b0b7ce43f3"
_LOCK = RLock()


@lru_cache(maxsize=1)
def templates() -> dict:
    path = Path(os.environ.get("SPDX_TEMPLATE_PATH", "/opt/spdx/templates.json"))
    data = json.loads(path.read_text(encoding="utf-8"))
    if data["version"] != DATA_VERSION or data["commit"] != DATA_COMMIT:
        raise ValueError("SPDX template data version differs from the detector")
    return data["licenses"]


@lru_cache(maxsize=1)
def _comparator():
    # Start lazily in a Celery child, never in the prefork parent or Web process.
    import jpype

    with _LOCK:
        if not jpype.isJVMStarted():
            jar = Path(os.environ.get("SPDX_TOOLS_JAR", "/opt/spdx/tools-java.jar"))
            if hashlib.sha256(jar.read_bytes()).hexdigest() != TOOLS_SHA256:
                raise ValueError("SPDX tools JAR checksum differs from this detector")
            jpype.startJVM("-Xmx256m", classpath=[str(jar)])
        return jpype.JClass("org.spdx.utility.compare.LicenseCompareHelper")


def _plain_text(text: str) -> str:
    """Remove routine Markdown decoration without discarding any prose."""
    text = unicodedata.normalize("NFC", text.removeprefix("\ufeff"))
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    text = re.sub(r"(?<!\w)(\*\*|__|\*|_|`)(?=\S)(.+?)(?<!\s)\1(?!\w)", r"\2", text)
    return text


def matching_templates(text: str, identifiers) -> list[str]:
    data = templates()
    compare = _comparator()
    text = unicodedata.normalize("NFC", text.removeprefix("\ufeff"))
    plain = _plain_text(text)
    # Some official templates (including PolyForm) contain literal Markdown.
    # Always compare the original before trying optional decoration removal.
    return [
        identifier for identifier in sorted(set(identifiers).intersection(data))
        if not compare.isTextMatchingTemplate(data[identifier]["template"], text).isDifferenceFound()
        or (plain != text and not compare.isTextMatchingTemplate(
            data[identifier]["template"], plain,
        ).isDifferenceFound())
    ]
