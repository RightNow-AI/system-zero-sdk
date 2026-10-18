#!/usr/bin/env python3
"""Stage public SDK package inputs without tests, installs or network access.

Python 3.11+; standard library only. Build tools consume the returned npm/ and
python/ trees so repository tests cannot enter a distribution archive.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import tomllib


def linked(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def ancestors(path: Path) -> None:
    for parent in reversed(path.parents):
        if os.path.lexists(parent):
            info = parent.lstat()
            if linked(info) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("Package paths must not cross directory links")


def regular(path: Path, *, directory: bool = False) -> None:
    ancestors(path)
    info = path.lstat()
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if linked(info) or not expected:
        raise ValueError("Expected a regular package input")


def read(path: Path) -> str:
    regular(path)
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def sources(root: Path, suffixes: set[str]) -> list[Path]:
    regular(root, directory=True)
    found: list[Path] = []
    for path in sorted(root.iterdir()):
        if path.name in {"tests", "node_modules", "__pycache__", ".pytest_cache"}:
            continue
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            found.extend(sources(path, suffixes))
        else:
            regular(path)
            if path.suffix in suffixes and not path.name.endswith((".test.ts", ".spec.ts")):
                found.append(path)
    return found


def prepare(root: Path, output: Path) -> None:
    regular(root, directory=True)
    ancestors(output)
    if os.path.lexists(output):
        raise ValueError("Output must be a new directory")
    if any(output.is_relative_to(root / name) for name in ("typescript", "python")):
        raise ValueError("Output must not overlap the package source trees")
    for name in ("typescript", "python"):
        regular(root / name, directory=True)
    staged: dict[str, str] = {}
    for destination, source in (("npm", "typescript"), ("python", "python")):
        for filename in ("README.md", "LICENSE", "NOTICE"):
            staged[f"{destination}/{filename}"] = read(root / source / filename)
    staged["npm/package.json"] = read(root / "typescript/package.json")
    staged["python/pyproject.toml"] = read(root / "python/pyproject.toml")
    npm = json.loads(staged["npm/package.json"])
    python = tomllib.loads(staged["python/pyproject.toml"])["project"]
    if (npm.get("name") != "@rightnow/system-zero" or python.get("name") != "rightnow-system-zero"
            or npm.get("version") != python.get("version")):
        raise ValueError("SDK package identities must agree before package preparation")
    config = json.loads(read(root / "typescript/tsconfig.json"))
    config["compilerOptions"]["typeRoots"] = [(root / "typescript/node_modules/@types").as_posix()]
    staged["npm/tsconfig.json"] = json.dumps(config, indent=2) + "\n"
    for destination, source, suffixes in (
        ("npm", "typescript", {".ts"}), ("python", "python", {".py", ".typed", ".json"}),
    ):
        source_root = root / source / "src"
        for path in sources(source_root, suffixes):
            staged[f"{destination}/src/{path.relative_to(source_root).as_posix()}"] = read(path)
    if "python/src/system_zero/__init__.py" not in staged:
        raise ValueError("The Python import package must be present")
    for filename, value in sorted(staged.items()):
        path = output / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8", newline="\n")
    print(json.dumps({"version": npm["version"], "files": sorted(staged)}, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare(Path(os.path.abspath(args.root)), Path(os.path.abspath(args.output)))
    except (OSError, ValueError, KeyError, TypeError):
        print("Package preparation refused invalid or incomplete inputs.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
