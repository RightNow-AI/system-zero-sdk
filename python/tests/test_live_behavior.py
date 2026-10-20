"""Ordinary mocked HTTP and local helper behavior."""

import json
import tempfile
import unittest
from pathlib import Path

import httpx
import system_zero as sdk
import system_zero.tools as tools


REQUEST = {
    "model": "system-zero", "state": "SYNTHETIC state",
    "questions": {
        "zebra": {"type": "boolean", "instructions": "Is this synthetic?"},
        "alpha": {"type": "boolean", "instructions": "Is this a test?"},
    },
}
TOOL_ARGUMENTS = {
    "state": {"json": '{"status":"ready"}'},
    "questions": [{"name": "ready", "type": "boolean", "instructions": "Is the status ready?"}],
}
CALL_LIMIT_REFUSAL = {
    "status": "error", "request_id": None,
    "error": {"code": "call_limit_reached", "message": "The local call limit has been reached."},
}


class LiveBehaviorTests(unittest.TestCase):
    def test_request_order_identity_headers_and_separate_response_metadata(self):
        reply = {"id": "szd_00000000000000000000000000000000", "answers": {}}
        seen = []

        def send(request):
            seen.append(request)
            return httpx.Response(200, json=reply, headers={
                "X-Request-Id": "req_synthetic", "X-Client-Request-Id": "client-synthetic",
                "X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "99", "X-RateLimit-Reset": "1800000000",
                "X-Latency-Ms": "12.5", "X-RightNow-Idempotent-Replay": "true",
            })

        with sdk.SystemZero(api_key="SYNTHETIC", transport=httpx.MockTransport(send), max_retries=0) as client:
            result = client.decide_with_response(
                REQUEST, idempotency_key="stable-synthetic-key", client_request_id="client-synthetic",
                headers={"x-synthetic": "preserved"},
            )
        self.assertEqual(len(seen), 1)
        request = seen[0]
        self.assertEqual((request.method, request.url.path), ("POST", "/v1/system-zero/decide"))
        self.assertEqual(json.loads(request.content), REQUEST)
        self.assertEqual(list(json.loads(request.content)["questions"]), ["zebra", "alpha"])
        self.assertEqual(request.headers["authorization"], "Bearer SYNTHETIC")
        self.assertEqual(request.headers["idempotency-key"], "stable-synthetic-key")
        self.assertEqual(request.headers["x-client-request-id"], "client-synthetic")
        self.assertEqual(request.headers["x-synthetic"], "preserved")
        self.assertEqual(result.data, reply)
        self.assertEqual(set(result.data), set(reply))
        metadata = result.metadata
        self.assertEqual((metadata.request_id, metadata.client_request_id, metadata.latency_ms, metadata.replayed),
                         ("req_synthetic", "client-synthetic", 12.5, True))
        self.assertEqual((metadata.rate_limit.limit, metadata.rate_limit.remaining, metadata.rate_limit.reset),
                         (100, 99, 1800000000))

    def test_zero_count_batch_stops_before_transport(self):
        scratch_root = Path(__file__).resolve().parents[2] / "tmp"
        scratch_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="live-batch-", dir=scratch_root) as directory:
            with sdk.SystemZero(api_key="SYNTHETIC", max_retries=0, transport=httpx.MockTransport(
                    lambda _request: self.fail("unexpected transport"))) as client:
                result = sdk.run_batch(client, [{"id": "one", "request": REQUEST,
                                               "expected": {"zebra": True, "alpha": True}}],
                                       run_id="synthetic-count", results_path=Path(directory) / "results.jsonl",
                                       max_decide_calls=0, deadline_ms=1000, concurrency=1)
            self.assertEqual(result, {"records": [], "stopped": "call_limit", "decide_calls": 0})

    def test_local_call_admission_refuses_before_transport(self):
        catalog = json.loads(Path(tools.__file__).with_name("error-catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(len(catalog), 71)
        self.assertNotIn("call_limit_reached", catalog)
        seen = []
        with sdk.SystemZero(api_key="SYNTHETIC", max_retries=0, transport=httpx.MockTransport(
                lambda request: (seen.append(request), httpx.Response(200, json={"answers": {}}))[1])) as client:
            handler = tools.create_tool_handler(client, max_decide_calls=0)
            self.assertEqual(handler("system_zero_decide", TOOL_ARGUMENTS), CALL_LIMIT_REFUSAL)
            self.assertEqual(handler.budget, {"decide_calls": 0})
        self.assertEqual(seen, [])

    def test_call_count_remains_after_limit_exhaustion(self):
        seen = []

        def send(request):
            seen.append(request)
            return httpx.Response(200, json={"answers": {}}, headers={"x-request-id": "req_synthetic_count"})

        with sdk.SystemZero(api_key="SYNTHETIC", transport=httpx.MockTransport(send), max_retries=0) as client:
            handler = tools.create_tool_handler(client, max_decide_calls=1)
            self.assertEqual(handler("system_zero_decide", TOOL_ARGUMENTS), {
                "answers": {}, "status": "ok", "request_id": "req_synthetic_count",
            })
            for _ in range(2):
                self.assertEqual(handler("system_zero_decide", TOOL_ARGUMENTS), CALL_LIMIT_REFUSAL)
                self.assertEqual(len(seen), 1)
                self.assertEqual(handler.budget, {"decide_calls": 1})

    def test_local_admission_code_remains_unknown_in_remote_errors(self):
        remote_error = {"code": "call_limit_reached", "type": "permission_error",
                        "message": "SYNTHETIC remote refusal.", "retryable": False}
        catalog = json.loads(Path(tools.__file__).with_name("error-catalog.json").read_text(encoding="utf-8"))
        for status, body in ((403, {"error": remote_error}), (200, {"answers": {"ready": {"error": remote_error}}})):
            with self.subTest(status=status):
                with sdk.SystemZero(api_key="SYNTHETIC", max_retries=0, transport=httpx.MockTransport(
                        lambda _request: httpx.Response(status, json=body))) as client:
                    handler = tools.create_tool_handler(client, max_decide_calls=1)
                    result = handler("system_zero_decide", TOOL_ARGUMENTS)
                    self.assertEqual((result["status"], result["request_id"]), ("error", None))
                    self.assertEqual(result["error"], {"code": "system_zero_unavailable",
                                                     "message": catalog["system_zero_unavailable"]["message"]})
                    self.assertEqual(handler.budget, {"decide_calls": 1})

    def test_live_gateway_refusals_are_preserved(self):
        for code, status in (("invalid_api_key", 401), ("monthly_cap_reached", 429), ("idempotency_conflict", 409),
                             ("auth_error", 403), ("account_suspended", 403), ("model_not_found", 404)):
            with self.subTest(code=code):
                with sdk.SystemZero(api_key="SYNTHETIC", max_retries=0, transport=httpx.MockTransport(
                        lambda _request: httpx.Response(status, json={"request_id": "synthetic-request", "error": {
                            "code": code, "type": "api_error", "message": "Synthetic refusal.", "retryable": False}}))) as client:
                    handler = tools.create_tool_handler(client)
                    self.assertEqual(handler("system_zero_list_models", {}), {"status": "error",
                        "request_id": "synthetic-request", "error": {"code": code, "message": "Synthetic refusal."}})


    def test_stateless_refusal_catalogs_preserve_classes_retry_flags_and_open_codes(self):
        catalog = json.loads(Path(tools.__file__).with_name("error-catalog.json").read_text(encoding="utf-8"))
        cases = [['attachment_too_large', 413, 'invalid_request_error', 'InvalidRequestError', False], ['document_controls_not_available', 404, 'not_found_error', 'NotFoundError', False], ['document_decode_failed', 422, 'invalid_request_error', 'InvalidRequestError', False], ['document_pictures_not_available', 404, 'not_found_error', 'NotFoundError', False], ['document_too_large', 413, 'invalid_request_error', 'InvalidRequestError', False], ['document_unsupported', 415, 'invalid_request_error', 'InvalidRequestError', False], ['long_duration_unsupported', 413, 'invalid_request_error', 'InvalidRequestError', False], ['long_evidence_incomplete', 422, 'invalid_request_error', 'InvalidRequestError', False], ['long_media_guard_refused', 422, 'invalid_request_error', 'InvalidRequestError', False], ['long_no_readable_window', 422, 'invalid_request_error', 'InvalidRequestError', False], ['long_options_unsupported', 422, 'invalid_request_error', 'InvalidRequestError', False], ['long_phrase_invalid', 422, 'invalid_request_error', 'InvalidRequestError', False], ['long_question_type_unsupported', 422, 'invalid_request_error', 'InvalidRequestError', False], ['long_question_unsupported', 422, 'invalid_request_error', 'InvalidRequestError', False], ['long_state_unsupported', 400, 'invalid_request_error', 'InvalidRequestError', False], ['long_too_many_phrases', 413, 'invalid_request_error', 'InvalidRequestError', False], ['media_decode_failed', 422, 'invalid_request_error', 'InvalidRequestError', False], ['media_too_large', 413, 'invalid_request_error', 'InvalidRequestError', False], ['media_unpriced', 403, 'permission_error', 'PermissionError', False], ['media_unsupported', 400, 'invalid_request_error', 'InvalidRequestError', False], ['staging_attempt_ceiling', 429, 'overloaded_error', 'OverloadedError', True], ['upload_in_use', 409, 'invalid_request_error', 'InvalidRequestError', True], ['upload_not_found', 404, 'not_found_error', 'NotFoundError', False], ['upload_not_ready', 409, 'invalid_request_error', 'InvalidRequestError', False], ['insufficient_credits', 402, 'permission_error', 'PermissionError', False]]
        for code, status, kind, class_name, retryable in cases:
            with self.subTest(code=code):
                self.assertIn(code, sdk.KNOWN_ERROR_CODES)
                self.assertEqual(sdk.ErrorCode(code).value, code)
                self.assertEqual(catalog[code]["status"], status)
                seen = []
                def send(request):
                    seen.append(request)
                    return httpx.Response(status, json={"error": {
                        "code": code, "type": kind, "message": "Synthetic refusal.", "retryable": retryable}})
                with sdk.SystemZero(api_key="SYNTHETIC", transport=httpx.MockTransport(send)) as client:
                    with self.assertRaises(getattr(sdk, class_name)) as caught:
                        client.decide(REQUEST)
                    self.assertEqual((caught.exception.code, caught.exception.status, caught.exception.retryable),
                                     (code, status, retryable))
                    self.assertEqual(len(seen), 1)
                    result = tools.create_tool_handler(client)("system_zero_decide", TOOL_ARGUMENTS)
                    self.assertEqual(result["error"], {"code": code, "message": "Synthetic refusal."})
                    self.assertEqual(result["status"], "held" if code in
                                     ("document_controls_not_available", "document_pictures_not_available") else "error")
                    self.assertEqual(len(seen), 2)
        for status, kind, class_name, retryable in ((422, "invalid_request_error", "InvalidRequestError", False),
                                                   (503, "api_error", "APIError", True)):
            with self.subTest(future_status=status):
                seen = []
                def send_future(request):
                    seen.append(request)
                    return httpx.Response(status, json={"error": {
                        "code": "future_refusal", "type": kind,
                        "message": "Synthetic refusal.", "retryable": retryable}})
                with sdk.SystemZero(api_key="SYNTHETIC", transport=httpx.MockTransport(send_future)) as client:
                    with self.assertRaises(getattr(sdk, class_name)) as caught:
                        client.decide(REQUEST)
                    self.assertEqual((caught.exception.code, caught.exception.status, caught.exception.retryable),
                                     ("future_refusal", status, retryable))
                    self.assertEqual(len(seen), 1)

    def test_secure_base_is_accepted_and_insecure_remote_base_is_refused(self):
        with self.assertRaises(ValueError):
            sdk.SystemZero(api_key="SYNTHETIC", base_url="http://sdk.example.com/v1")
        with sdk.SystemZero(api_key="SYNTHETIC", base_url="https://sdk.example.com/v1", transport=httpx.MockTransport(
                lambda _request: self.fail("unexpected transport"))):
            pass

    def test_tool_output_redacts_injected_credentials_and_bearer_values(self):
        api_key = "SYNTHETIC_KEY"
        shaped = "rnsk_" + "Q" * 40
        echo = api_key + " Bearer SYNTHETIC_BEARER " + shaped
        with sdk.SystemZero(api_key=api_key, max_retries=0, transport=httpx.MockTransport(
                lambda _request: httpx.Response(400, json={"request_id": echo, "error": {
                    "code": "invalid_request", "type": "invalid_request_error", "message": echo, "retryable": False}}))) as client:
            result = tools.create_tool_handler(client)("system_zero_list_models", {})
        self.assertEqual(result["error"]["code"], "invalid_request")
        for secret in (api_key, "SYNTHETIC_BEARER", shaped):
            self.assertNotIn(secret, json.dumps(result))


if __name__ == "__main__":
    unittest.main()
