"""One-time: copy the wheels published from wheelgap/tracker into RELEASE_DIR, checking each against the index.

    python3 scripts/migrate_from_tracker.py data/gap-wheels.json RELEASE_DIR

The first 12 gap wheels were published on wheelgap/tracker's `gap-wheels` release before the pipeline moved here.
Each is downloaded from there and kept only if its sha256 is the one data/gap-wheels.json (committed here) records;
any mismatch stops the whole run. The workflow then attests the files and uploads them to this repo's release.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

TRACKER_RELEASE = "https://github.com/wheelgap/tracker/releases/download/gap-wheels/"
ASSET = re.compile(r"[A-Za-z0-9._-]{1,200}\.whl", re.ASCII)
MAX_BYTES = 500_000_000


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError(f"{url}: bigger than {MAX_BYTES} bytes")
    return data


def migrate(index: Path, out: Path, get=fetch) -> int:
    wheels = json.loads(index.read_text(encoding="utf-8"))["wheels"]
    out.mkdir(parents=True, exist_ok=True)
    for w in wheels:
        name, expected = w["wheel"], w["sha256"]
        if not ASSET.fullmatch(name):
            raise ValueError(f"{name!r}: not a release asset name")
        data = get(TRACKER_RELEASE + name)
        got = hashlib.sha256(data).hexdigest()
        if got != expected:
            raise ValueError(f"{name}: sha256 {got} isn't the index's {expected}")
        (out / name).write_bytes(data)
        print(f"{name}: sha256 matches the index")
    return len(wheels)


if __name__ == "__main__":
    print(f"{migrate(Path(sys.argv[1]), Path(sys.argv[2]))} wheels ready to publish here")
