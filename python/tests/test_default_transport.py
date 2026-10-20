"""Loopback checks for the HTTP transport used without dependency injection."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import tempfile
from threading import Thread
import time

import httpx
import pytest

from system_zero import APIConnectionError, APIError, InvalidRequestError, SystemZero
from system_zero.batch import run_batch


HOST = ".".join(str(part) for part in (127, 0, 0, 1))
REQUEST = {"model": "system-zero", "state": "SYNTHETIC \u00e9",
           "questions": {"ready": {"type": "boolean", "instructions": "Ready?"}}}
MODELS = {"object": "list", "data": []}
DECISION = {"id": "szd_synthetic", "model": "system-zero", "served_by": "synthetic", "created": 0,
            "context": {"route": "direct", "tokens": 1}, "answers": {},
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "media_tokens": 0,
                      "long_input_tokens": 0, "media_items": 0, "thought_tokens": 0}}


@contextmanager
def server_for(respond):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def handle_request(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            seen.append({"method": self.command, "headers": dict(self.headers), "body": body})
            respond(self, len(seen))

        do_GET = handle_request
        do_POST = handle_request

    server = HTTPServer((HOST, 0), Handler)
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    try:
        yield "http://" + HOST + ":" + str(server.server_port) + "/v1", seen
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()


def reply(handler, body, status=200):
    payload = json.dumps(body).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def client(base_url, timeout=0.3):
    return SystemZero("SYNTHETIC", base_url=base_url, timeout=timeout, max_retries=1,
                      retry_budget=5, random=lambda: 0, sleep=lambda _: None)


def call(sdk, method):
    return sdk.models.list() if method == "GET" else sdk.decide(REQUEST, idempotency_key="SYNTHETIC")


def test_default_transport_decide_and_models_framing():
    with server_for(lambda handler, _: reply(handler, MODELS if handler.command == "GET" else DECISION)) as (base_url, seen):
        with client(base_url) as sdk:
            assert sdk.decide(REQUEST, idempotency_key="SYNTHETIC") == DECISION
            assert sdk.models.list() == MODELS
        assert [row["method"] for row in seen] == ["POST", "GET"]
        assert seen[0]["headers"]["Content-Length"] == str(len(seen[0]["body"]))
        assert "Transfer-Encoding" not in seen[0]["headers"]
        assert json.loads(seen[0]["body"]) == REQUEST
        assert seen[1]["body"] == b""


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_default_transport_retries_before_any_response_byte(method):
    def respond(handler, count):
        if count == 1:
            handler.close_connection = True
        else:
            reply(handler, MODELS if method == "GET" else DECISION)

    with server_for(respond) as (base_url, seen):
        with client(base_url) as sdk:
            call(sdk, method)
        assert len(seen) == 2
        assert [row["headers"].get("Idempotency-Key") for row in seen] == ([None, None] if method == "GET" else ["SYNTHETIC", "SYNTHETIC"])
        assert seen[0]["body"] == seen[1]["body"]


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_default_transport_never_replays_after_a_partial_header(method):
    def respond(handler, _):
        handler.wfile.write(b"H")
        handler.close_connection = True

    with server_for(respond) as (base_url, seen):
        with client(base_url) as sdk, pytest.raises(APIConnectionError) as caught:
            call(sdk, method)
        assert caught.value.retryable is False
        assert caught.value.request_sent is True
        assert len(seen) == 1


def test_default_transport_body_deadline_prevents_replay():
    def respond(handler, _):
        handler.send_response(200)
        handler.send_header("Content-Length", "100")
        handler.end_headers()
        handler.wfile.write(b"{")
        handler.wfile.flush()
        time.sleep(0.3)
        handler.close_connection = True

    with server_for(respond) as (base_url, seen):
        with client(base_url, timeout=0.1) as sdk:
            started = time.monotonic()
            with pytest.raises(APIConnectionError) as caught:
                sdk.models.list()
            assert time.monotonic() - started < 2
        assert caught.value.type == "timeout_error"
        assert caught.value.retryable is False
        assert caught.value.request_sent is True
        assert caught.value.status == 200
        assert len(seen) == 1


@pytest.mark.parametrize("status", [600, 999])
def test_default_transport_nonstandard_status_is_an_api_error(status):
    with server_for(lambda handler, _: reply(handler, {}, status)) as (base_url, seen):
        with client(base_url) as sdk, pytest.raises(APIError) as caught:
            sdk.models.list()
        assert caught.value.status == status
        assert len(seen) == 1


@pytest.mark.parametrize("method", ["GET", "POST", "batch"])
def test_mutated_base_url_is_refused_before_dispatch_or_receipt_creation(method):
    seen = []

    def transport(request):
        seen.append(request)
        return httpx.Response(200, json=MODELS)

    scratch = Path.cwd() / "tmp"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="target-test-", dir=scratch) as temporary:
        results_path = Path(temporary) / "receipts.jsonl"
        with SystemZero("SYNTHETIC", max_retries=0, transport=httpx.MockTransport(transport)) as sdk:
            sdk.base_url = "http://sdk.example.com/v1"
            with pytest.raises((ValueError, InvalidRequestError)):
                if method == "batch":
                    run_batch(sdk, [], run_id="synthetic", results_path=results_path,  deadline_ms=1000)
                else:
                    call(sdk, method)
        assert not seen
        assert not results_path.exists()
        assert not Path(str(results_path) + ".admissions.jsonl").exists()
