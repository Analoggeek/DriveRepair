#!/usr/bin/env python3
"""Turn a JUnit report into GitHub annotations.

A failing CI job reports only an exit code in its log, and job logs are served
from a storage host that is not always reachable. Annotations travel over the
normal API, so writing each failure as ``::error`` makes a red build explain
itself: which test, and the assertion that broke.

Usage: report_failures.py junit.xml [pytest.log]
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# Long tracebacks are truncated: annotations are for diagnosis, not for a full
# log dump, and an oversized annotation is dropped by the runner.
MAX_CHARS = 1200


def clip(text: str, limit: int = MAX_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def escape(text: str) -> str:
    """GitHub workflow commands are line-based and escape % , CR and LF."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(argv: list[str]) -> int:
    if not argv:
        print("::error::report_failures.py needs a JUnit XML path")
        return 2

    report = Path(argv[0])
    if not report.exists():
        print(f"::error::no JUnit report at {report} — the test step crashed before it could write one")
        if len(argv) > 1 and Path(argv[1]).exists():
            tail = Path(argv[1]).read_text(encoding="utf-8", errors="replace")[-MAX_CHARS:]
            print(f"::error::last output: {escape(clip(tail))}")
        return 1

    try:
        tree = ET.parse(report)
    except ET.ParseError as exc:
        print(f"::error::unreadable JUnit report {report}: {exc}")
        return 1

    failures = 0
    for case in tree.iter("testcase"):
        for kind in ("failure", "error"):
            node = case.find(kind)
            if node is None:
                continue
            failures += 1
            name = case.get("name") or "<unnamed test>"
            classname = case.get("classname") or ""
            path = (case.get("file") or "").replace("\\", "/")
            # pytest leaves `message` empty for a collection error, and the
            # useful part (which import failed and why) is in the element text.
            detail = clip(node.get("message") or "") or clip(node.text or "")
            label = f"{classname}.{name}" if classname else name
            if kind == "error" and not classname:
                # A collection error is filed under the module being collected.
                label = f"collection of {name}"
            # pytest only emits the `file` attribute in some versions; an empty
            # "file=" would make the whole command malformed.
            location = f" file={path}" if path else ""
            print(f"::error{location}::FAILED {label}: {escape(detail)}")

    if not failures:
        print("::error::the test step failed but the JUnit report contains no failing test "
              "(collection error or crash)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
