#!/usr/bin/env python3
"""One reviewed policy for staged metadata and eligibility to publish either SDK."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


# This destination identifies the public SDK repository.
# Publishing requires the configured SDK repository.
SDK_SOURCE_PUBLIC = True
PUBLIC_CI_MODE = 'hosted'
PUBLIC_SDK_REPOSITORY = "https://github.com/RightNow-AI/system-zero-sdk.git"
PUBLISH_WAIT = "Publishing waits for the public SDK repository."


class ReleaseRuleError(ValueError):
    """A redacted publication-policy refusal, safe to include in gate output."""


def public_repository() -> str:
    if type(SDK_SOURCE_PUBLIC) is not bool:
        raise ReleaseRuleError("Invalid public SDK repository source policy.")
    if not SDK_SOURCE_PUBLIC:
        return ""
    if not isinstance(PUBLIC_SDK_REPOSITORY, str) or re.fullmatch(
        r"https:" r"//" r"github\.com/[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+\.git",
        PUBLIC_SDK_REPOSITORY,
    ) is None:
        raise ReleaseRuleError("Configure the canonical public SDK repository URL before publishing.")
    slug = PUBLIC_SDK_REPOSITORY.removeprefix("https://github.com/").removesuffix(".git")
    if slug.split("/", 1)[1] in {".", ".."}:
        raise ReleaseRuleError("The configured repository must identify the approved SDK repository.")
    return PUBLIC_SDK_REPOSITORY


def check_npm_repository(package: dict[str, object]) -> None:
    expected = public_repository()
    if not SDK_SOURCE_PUBLIC:
        if "repository" in package:
            raise ReleaseRuleError("Repository metadata is omitted when publishing is disabled.")
        return
    repository = package.get("repository")
    if not isinstance(repository, dict) or repository.get("url") != expected:
        raise ReleaseRuleError("Artifact repository URL must exactly match the configured public SDK repository.")


def require_public(repository: str) -> None:
    expected = public_repository()
    if not SDK_SOURCE_PUBLIC:
        raise ReleaseRuleError(PUBLISH_WAIT)
    slug = expected.removeprefix("https://github.com/").removesuffix(".git")
    if repository != slug:
        raise ReleaseRuleError("Publication must run from the configured public SDK repository.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--require-public", action="store_true")
    parser.add_argument("--repository", default="")
    parser.add_argument("--repository-url", action="store_true")
    args = parser.parse_args()
    try:
        public_repository()
        if args.repository_url:
            if not SDK_SOURCE_PUBLIC:
                raise ReleaseRuleError(PUBLISH_WAIT)
            print(public_repository())
        if args.require_public:
            require_public(args.repository)
        if args.github_output is not None:
            with args.github_output.open("a", encoding="ascii", newline="\n") as output:
                output.write("public=" + str(SDK_SOURCE_PUBLIC).lower() + "\n")
    except ReleaseRuleError as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print("Unable to record the public SDK repository policy.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
