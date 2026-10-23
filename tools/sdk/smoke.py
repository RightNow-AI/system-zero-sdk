#!/usr/bin/env python3
"""Exercise public Python entrypoints and an injected HTTP transport offline."""

from importlib.metadata import version as installed_version
import json
import os
import re
from pathlib import Path
import sys


installed = len(sys.argv) > 1 and sys.argv[1] == "--installed"
root = None if installed else Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2] / "python"
if root is not None:
    sys.path.insert(0, str(root.resolve() / "src"))

import httpx  # noqa: E402
import system_zero  # noqa: E402
import system_zero.tools  # noqa: E402
from system_zero.stream import SystemZeroStreamClient  # noqa: E402


expected_python = os.environ.get("SDK_EXPECTED_PYTHON")
if expected_python:
    assert tuple(map(int, expected_python.split("."))) == sys.version_info[:2]
if installed:
    assert system_zero.__version__ == installed_version("rightnow-system-zero")
    assert Path(system_zero.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
else:
    metadata = (root / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'^version\s*=\s*"([^"\n]+)"', metadata, re.MULTILINE)
    assert version is not None and system_zero.__version__ == version[1]


request_body = {
    "model": "system-zero", "state": "SYNTHETIC package smoke.",
    "questions": {"ready": {"type": "boolean", "instructions": "Is this a package smoke?"}},
}
reply_body = {"id": "szd_" + "a" * 32, "answers": {}}
calls = []


def send(request):
    calls.append(request)
    assert request.method == "POST"
    assert json.loads(request.content) == request_body
    assert request.headers["idempotency-key"] == "smoke-request"
    return httpx.Response(200, json=reply_body)


with system_zero.SystemZero(api_key="public-smoke", max_retries=0, transport=httpx.MockTransport(send)) as client:
    assert client.decide(request_body, idempotency_key="smoke-request") == reply_body
assert len(calls) == 1
assert callable(SystemZeroStreamClient)
assert callable(system_zero.tools.create_tool_handler)
assert system_zero.evaluate([])["items"]["total"] == 0
print("Python SDK public entrypoints and injected transport passed.")
