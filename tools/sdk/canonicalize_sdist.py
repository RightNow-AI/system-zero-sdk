#!/usr/bin/env python3
"""Remove only the standard tar writer's generated gzip filename before checks."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
import tempfile
import zlib


# Keep the same physical-container bound as check_release.py without loading its scanner.
MAX_CONTAINER_BYTES = 64 * 1024 * 1024 + 10000 * 1024 + 65536
SDIST_NAME = re.compile(r"rightnow_system_zero-(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.tar\.gz")


def canonicalize(path: Path) -> None:
    """Preserve the DEFLATE stream and trailer; leave canonical files untouched."""
    if SDIST_NAME.fullmatch(path.name) is None or path.is_symlink() or not path.is_file():
        raise ValueError("invalid sdist input")
    with path.open("rb") as source:
        raw = source.read(MAX_CONTAINER_BYTES + 1)
    if len(raw) > MAX_CONTAINER_BYTES or len(raw) < 18 or raw[:3] != b"\x1f\x8b\x08" or raw[3] not in (0, 8):
        raise ValueError("unsupported gzip wrapper")

    offset = 10
    if raw[3] == 8:
        expected = path.name.removesuffix(".gz").encode("ascii") + b"\0"
        if raw[offset:offset + len(expected)] != expected:
            raise ValueError("unexpected gzip filename")
        offset += len(expected)

    # zlib checks the original trailer and stops at one member. Never merge or
    # discard extra streams or bytes while preparing an artifact for the gate.
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        expanded = decoder.decompress(raw, MAX_CONTAINER_BYTES + 1)
    except zlib.error:
        raise ValueError("invalid gzip stream") from None
    if len(expanded) > MAX_CONTAINER_BYTES or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("invalid or oversized gzip stream")
    if raw[3] == 0:
        return

    # FNAME and mtime are build metadata. The strict artifact checker still runs
    # on the resulting bytes before upload and before each publisher.
    result = raw[:3] + b"\0" * 5 + raw[8:10] + raw[offset:]
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=path.name + ".", suffix=".tmp", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(result)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdist", type=Path, required=True)
    args = parser.parse_args()
    try:
        canonicalize(args.sdist)
    except (OSError, ValueError):
        print("sdist wrapper: normalization refused", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
