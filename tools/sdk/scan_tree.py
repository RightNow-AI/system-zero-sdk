#!/usr/bin/env python3
"""Check tracked public source files without printing their names or contents."""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import importlib.util
from pathlib import Path, PurePosixPath
import subprocess


ROOT = Path(__file__).resolve().parents[2]
MAX_FILES = 4096
MAX_FILE_BYTES = 16 * 1024 * 1024


def load_rules():
    spec = importlib.util.spec_from_file_location("public_tree_rules", ROOT / "tools/sdk/public_content_rules.py")
    if spec is None or spec.loader is None:
        raise ValueError("Content rules are unavailable.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _safe_name(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        return False
    path = PurePosixPath(name)
    return (not path.is_absolute() and path.as_posix() == name and ".." not in path.parts
            and ".git" not in path.parts and all(ord(character) >= 32 and ord(character) != 127 for character in name))


def _text(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_FILE_BYTES:
        raise ValueError("Unsupported content.")
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        raise ValueError("Unsupported encoding.")
    value = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
    if any((ord(character) < 32 and character not in "\t\r\n") or 127 <= ord(character) < 160 for character in value):
        raise ValueError("Unsupported content.")
    return value


def _generated_path_rule_ids(path):
    parts = PurePosixPath(path.casefold()).parts
    generated = {"__pycache__", "node_modules", "dist", "build", "tmp", ".venv", "venv", ".pytest_cache",
                 ".mypy_cache", ".ruff_cache", ".cache", "coverage", "htmlcov", ".nyc_output", "lcov-report"}
    if any(part in generated or part.endswith(".egg-info") for part in parts):
        return ["generated-path"]
    if parts and (parts[-1].endswith((".pyc", ".pyo", ".whl", ".tgz", ".tar.gz", ".zip", ".tsbuildinfo"))
                  or parts[-1].startswith('.coverage') or parts[-1] in {
                      'coverage.xml', 'lcov.info', 'coverage-final.json', 'coverage-summary.json',
                      'cobertura-coverage.xml', 'clover.xml'}):
        return ["generated-path"]
    return []


def scan_mapping(files, *, rules=None):
    """Return only rule identifiers for a mapping of relative paths to bytes."""
    rules = rules if rules is not None else load_rules()
    found = set()
    if len(files) > MAX_FILES:
        return ["scan-limit"]
    known = tuple(files)
    for name, raw in files.items():
        if not _safe_name(name):
            found.add("unsafe-path")
            continue
        found.update(_generated_path_rule_ids(name))
        sensitive = rules.sensitive_path_rule_ids(name)
        if sensitive:
            found.update(sensitive)
            continue
        found.update(rules.public_rule_ids(name, name, known_paths=known))
        try:
            value = _text(raw)
        except (ValueError, UnicodeError):
            found.add("binary-content")
            continue
        found.update(rules.public_rule_ids(value, name, known_paths=known))
    return sorted(found)


def _linked(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def _tracked_names(root):
    probe = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True,
                           timeout=15, check=False)
    if probe.returncode != 0:
        return None
    try:
        if Path(probe.stdout.decode("utf-8").strip()).resolve() != root:
            return None
    except (ValueError, UnicodeError):
        raise ValueError("Repository identity could not be checked.") from None
    result = subprocess.run(["git", "-C", str(root), "ls-files", "--cached", "-z"], capture_output=True,
                            timeout=15, check=False)
    if result.returncode != 0:
        raise ValueError("Tracked files could not be read.")
    return tuple(name for name in result.stdout.decode("utf-8").split("\0") if name)


def read_tree(root):
    """Read tracked files, or the complete directory for a fresh exported tree."""
    root = Path(root).absolute()
    if _linked(root) or root.resolve() != root or not root.is_dir():
        raise ValueError("The source directory is not regular.")
    rules = load_rules()
    names = _tracked_names(root)
    if names is None:
        names = []
        pending = [root]
        while pending:
            directory = pending.pop()
            for path in directory.iterdir():
                if path.name == ".git":
                    continue
                if _generated_path_rule_ids(path.relative_to(root).as_posix()):
                    raise ValueError('Generated content is refused.')
                if rules.sensitive_path_rule_ids(path.name):
                    raise ValueError("Sensitive file names are refused.")
                if _linked(path):
                    raise ValueError("Linked content is refused.")
                if path.is_dir():
                    pending.append(path)
                elif path.is_file():
                    names.append(path.relative_to(root).as_posix())
                else:
                    raise ValueError("Nonregular content is refused.")
                if len(names) + len(pending) > MAX_FILES:
                    raise ValueError("The source directory exceeds scan limits.")
    result = {}
    for name in names:
        if not _safe_name(name):
            raise ValueError("Unsafe tracked path.")
        if rules.sensitive_path_rule_ids(name):
            raise ValueError("Sensitive file names are refused.")
        path = root.joinpath(*PurePosixPath(name).parts)
        cursor = path
        while cursor != root:
            if _linked(cursor):
                raise ValueError("Linked content is refused.")
            cursor = cursor.parent
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("Unsupported file.")
        result[name] = path.read_bytes()
    return result


class Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise ValueError("Arguments were refused.")


def main(argv=None):
    try:
        parser = Parser(description="Check public repository content without displaying scanned data.")
        parser.add_argument("--root", type=Path, default=ROOT)
        arguments = parser.parse_args(argv)
        files = read_tree(arguments.root)
        findings = scan_mapping(files)
        if findings:
            print("Public tree content check refused: " + ", ".join(findings), file=sys.stderr)
            return 1
        print("Public tree content check passed: " + str(len(files)) + " files")
        return 0
    except Exception:
        print("Public tree content check could not complete; input details withheld.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
