#!/usr/bin/env python3
"""Bind downloaded package checksums to the build job's independent digest."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import stat
import sys


def manifest_digest(path):
    for parent in (path, *path.parents):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise ValueError("Manifest paths must be regular")
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Manifest must be a bounded regular file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(path, expected):
    if re.fullmatch(r"[0-9a-f]{64}", expected or "") is None or manifest_digest(path) != expected:
        raise ValueError("Artifact manifest digest differs from the build job")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--record", action="store_true")
    args = parser.parse_args()
    try:
        if args.record:
            digest = manifest_digest(Path(os.path.abspath(args.manifest)))
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="ascii", newline="\n") as output:
                output.write("manifest_sha256=" + digest + "\n")
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="ascii", newline="\n") as summary:
                summary.write("Artifact manifest SHA-256: `" + digest + "`\n")
        else:
            verify(Path(os.path.abspath(args.manifest)), os.environ.get("SDK_MANIFEST_SHA256", ""))
        print("Artifact manifest binding passed.")
        return 0
    except (OSError, ValueError, KeyError):
        print("Artifact manifest binding refused.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
