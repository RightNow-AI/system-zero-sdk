import httpx
import pytest
import json
from pathlib import Path
from system_zero import SystemZero, InvalidRequestError, LongContextNotAvailableError, video_attachment, run_batch, run_flow

REQUEST = {"model": "system-zero", "state": "sample", "questions": {"q": {"type": "boolean", "instructions": "Is it set?"}}}

def test_explicit_option_survives_retry_and_replay():
    seen = []
    def send(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(429, json={"error": {"code": "rate_limit_exceeded", "message": "Retry.", "retryable": True}})
        return httpx.Response(200, json={"answers": {}}, headers={"x-rightnow-idempotent-replay": "true"})
    with SystemZero(api_key="synthetic", transport=httpx.MockTransport(send), sleep=lambda _: None, random=lambda: 0) as client:
        client.decide_with_response(REQUEST, danger_zone="accept", idempotency_key="same")
    assert len(seen) == 2
    for request in seen:
        assert request.headers["SZ-Danger-Zone"] == "accept"
        assert request.headers["idempotency-key"] == "same"
        assert "x-system-zero-long-context" not in request.headers

@pytest.mark.parametrize("value", ["Accept", "", True, 1])
def test_invalid_option_before_transport(value):
    seen = []
    with SystemZero(api_key="synthetic", transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, json={}))) as client:
        with pytest.raises(InvalidRequestError):
            client.decide(REQUEST, danger_zone=value)
    assert not seen

@pytest.mark.parametrize("name", ["SZ-Danger-Zone", "sz-danger-zone", "sZ-dAnGeR-zOnE"])
def test_conflicting_header_before_transport(name):
    seen = []
    with SystemZero(api_key="synthetic", transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, json={}))) as client:
        for option in (None, "accept"):
            with pytest.raises(InvalidRequestError):
                client.decide(REQUEST, headers={name: "accept"}, danger_zone=option)
    assert not seen

def test_ordinary_helper_requests_and_large_states_add_no_opt_in():
    seen = []
    with SystemZero(api_key="synthetic", transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, json={"answers": {}}))) as client:
        data = bytes([0, 0, 0, 24]) + b"ftypisom" + bytes(12)
        client.decide({**REQUEST, "attachments": [video_attachment(name="clip", data=data)]})
        client.decide({**REQUEST, "state": "x" * 140000})
    assert all("SZ-Danger-Zone" not in request.headers for request in seen)

@pytest.mark.parametrize("rule", ["billing", "opt_in", "class", "deny_list", "global_switch", "account_deleted", "future_rule"])
def test_typed_eligibility_and_refusal_envelope(rule):
    seen = []
    def send(request):
        seen.append(request)
        return httpx.Response(404, json={"request_id": "req_held", "error": {"code": "long_context_not_available", "type": "not_found_error", "message": "This route is not available.", "details": {"eligibility": rule}}})
    with SystemZero(api_key="synthetic", transport=httpx.MockTransport(send)) as client:
        with pytest.raises(LongContextNotAvailableError) as caught:
            client.decide(REQUEST)
    error = caught.value
    assert (error.status, error.code, str(error), error.request_id, error.eligibility, error.retryable) == (404, "long_context_not_available", "This route is not available.", "req_held", rule, False)
    assert len(seen) == 1

@pytest.mark.parametrize("selected", ["accept", "long_context_64k", "long_context_128k", "long_context_1m"])
def test_batch_forwards_and_validates_option(tmp_path, selected):
    seen = []
    def send(request):
        seen.append(request)
        return httpx.Response(404, json={"error": {"code": "long_context_not_available", "type": "not_found_error", "message": "This route is not available."}})
    with SystemZero(api_key="synthetic", timeout=1, max_retries=0, transport=httpx.MockTransport(send)) as client:
        items = [{"id": "one", "request": REQUEST, "expected": {"q": True}}]
        options = dict(run_id="enabled", results_path=tmp_path / "rows.jsonl", deadline_ms=10000, concurrency=1)
        result = run_batch(client, items, danger_zone=selected, **options)
        assert result["records"][0]["outcome"] == "held"
        assert seen[0].headers["SZ-Danger-Zone"] == selected
        with pytest.raises(InvalidRequestError):
            run_batch(client, items, danger_zone="wrong", **options)
        if selected == "long_context_1m":
            options["results_path"].write_text("")
            run_batch(client, items, danger_zone=selected, **options)
            assert seen[1].headers["sz-danger-zone"] == selected
            assert seen[1].headers["idempotency-key"] == seen[0].headers["idempotency-key"]
            with pytest.raises(InvalidRequestError):
                run_batch(client, items, danger_zone="long_context_64k", **options)
    assert len(seen) == (2 if selected == "long_context_1m" else 1)

@pytest.mark.parametrize("selected", ["accept", "long_context_64k", "long_context_128k", "long_context_1m"])
def test_flow_forwards_option(selected):
    seen = []
    def send(request):
        seen.append(request)
        return httpx.Response(404, json={"error": {"code": "long_context_not_available", "type": "not_found_error", "message": "This route is not available."}})
    flow = {"version": 1, "request": {"model": REQUEST["model"], "state": REQUEST["state"]}, "steps": [{"id": "one", "questions": REQUEST["questions"], "next": {"rules": [], "default": None}}]}
    with SystemZero(api_key="synthetic", timeout=1, max_retries=0, transport=httpx.MockTransport(send)) as client:
        with pytest.raises(LongContextNotAvailableError):
            run_flow(client, flow, run_id="enabled", deadline_ms=10000, danger_zone=selected)
    assert seen[0].headers["SZ-Danger-Zone"] == selected

def test_enabled_request_requiredness():
    from typing import get_args
    from system_zero import LongContextRequest, EnabledDecideRequest, VideoRequest, LongContextLimits
    from system_zero.workflow_types import BatchItem
    assert LongContextRequest.__required_keys__ == frozenset({"model", "state", "questions"})
    assert "attachments" in LongContextRequest.__optional_keys__
    assert VideoRequest.__required_keys__ == frozenset({"model", "state", "questions", "attachments"})
    assert "choice_options" in LongContextLimits.__required_keys__
    assert EnabledDecideRequest is not None
    assert LongContextRequest in get_args(BatchItem.__annotations__["request"])
    assert VideoRequest in get_args(BatchItem.__annotations__["request"])

def test_discovery_refuses_reserved_header():
    seen = []
    with SystemZero(api_key="synthetic", transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, json={"data": []}))) as client:
        with pytest.raises(InvalidRequestError):
            client.models.list(headers={"sZ-Danger-Zone": "accept"})
    assert not seen

def test_discovery_long_context_range_is_optional():
    from system_zero.types import TextMediaChoiceRange
    assert TextMediaChoiceRange.__required_keys__ == frozenset({"text", "media"})
    assert TextMediaChoiceRange.__optional_keys__ == frozenset({"long_context"})


def test_ordinary_inline_pdf_and_video_use_exported_request_types_without_opt_in():
    import json
    from system_zero import EnabledDecideRequest, VideoRequest
    pdf: EnabledDecideRequest = {**REQUEST, "attachments": [{"name": "document", "type": "document", "data": "data:application/pdf;base64,JVBERi0xLjcK"}]}
    data = bytes([0, 0, 0, 24]) + b"ftypisom" + bytes(12)
    video: VideoRequest = {**REQUEST, "attachments": [video_attachment(name="clip", data=data)]}
    seen = []
    with SystemZero(api_key="synthetic", transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, json={"answers": {}}))) as client:
        client.decide(pdf)
        client.decide(video)
    assert [json.loads(request.content) for request in seen] == [pdf, video]
    assert all("SZ-Danger-Zone" not in request.headers for request in seen)

@pytest.mark.parametrize("selected", ["long_context_64k", "long_context_128k"])
def test_native_class_survives_retry_and_exposes_response_class(selected):
    seen = []
    def send(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(503, json={"error": {"code": "system_zero_unavailable", "retryable": True}}, headers={"Retry-After-Ms": "1"})
        return httpx.Response(200, json={"answers": {}, "usage": {"long_input_tokens": 48000}}, headers={"sz-danger-zone": selected})
    with SystemZero(api_key="SYNTHETIC", transport=httpx.MockTransport(send), random=lambda: 0, sleep=lambda _: None) as client:
        result = client.decide_with_response(REQUEST, danger_zone=selected)
    assert [request.headers["sz-danger-zone"] for request in seen] == [selected, selected]
    assert seen[0].headers["idempotency-key"] == seen[1].headers["idempotency-key"]
    assert result.data["usage"]["long_input_tokens"] == 48000
    assert result.metadata.danger_zone == [selected]

@pytest.mark.parametrize("selected", ["long_context_64k", "long_context_128k"])
def test_native_class_refuses_attachments_before_transport(selected):
    seen = []
    with SystemZero(api_key="SYNTHETIC", transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, json={}))) as client:
        with pytest.raises(LongContextNotAvailableError) as caught:
            client.decide({**REQUEST, "attachments": [{"name": "paper", "type": "document", "data": "data:application/pdf;base64,JVBERi0="}]}, danger_zone=selected)
    assert (caught.value.status, caught.value.code) == (404, "long_context_not_available")
    assert not seen

def test_discovery_keeps_direct_window_and_optional_long_bands():
    band = {"header_value": "long_context_64k", "min_state_tokens": 38001,
            "state_tokens": 65536, "rendered_tokens": 69632, "attachments": 0}
    direct = {"id": "system-zero", "limits": {"state_tokens": 38000, "rendered_tokens": 40960}}
    payload = {"object": "list", "data": [direct, {**direct, "limits": {**direct["limits"], "long_context": [band]}}]}
    def send(request):
        assert request.method == "GET" and request.url.path == "/v1/models"
        assert "sz-danger-zone" not in request.headers
        return httpx.Response(200, json=payload)
    with SystemZero(api_key="SYNTHETIC", transport=httpx.MockTransport(send), max_retries=0) as client:
        result = client.models.list()
    assert result == payload
    assert "long_context" not in result["data"][0]["limits"]
    assert result["data"][1]["limits"]["long_context"] == [band]


def test_model_long_bands_have_typed_required_fields():
    from typing import List, Literal, get_type_hints
    from system_zero.types import ModelLimits, ModelLongContextLimits
    assert "long_context" in ModelLimits.__optional_keys__
    assert get_type_hints(ModelLimits)["long_context"] == List[ModelLongContextLimits]
    expected = {"header_value": str, "min_state_tokens": int, "state_tokens": int,
                "rendered_tokens": int, "attachments": Literal[0]}
    assert get_type_hints(ModelLongContextLimits) == expected
    assert ModelLongContextLimits.__required_keys__ == set(expected)
def test_1m_generates_key_without_retries():
    seen = []
    with SystemZero(api_key="SYNTHETIC", max_retries=0, retry_budget=0,
                    transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, json={"answers": {}}))) as client:
        client.decide(REQUEST, danger_zone="long_context_1m")
    assert seen[0].headers["idempotency-key"]

def test_1m_requires_one_question_before_transport():
    seen = []
    with SystemZero(api_key="SYNTHETIC", transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, json={}))) as client:
        with pytest.raises(Exception) as caught:
            client.decide({**REQUEST, "questions": {}}, danger_zone="long_context_1m")
    assert (caught.value.code, caught.value.param) == ("questions_too_large", "questions")
    assert not seen

ONE_MILLION_CASES = json.loads((Path(__file__).resolve().parents[2] / "fixtures/long-context/1m.json").read_text())

@pytest.mark.parametrize("case", ONE_MILLION_CASES, ids=lambda case: case["name"])
def test_1m_local_shapes(case):
    seen = []
    with SystemZero(api_key="SYNTHETIC", transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, json={}))) as client:
        with pytest.raises(Exception) as caught:
            client.decide({**REQUEST, **case["patch"]}, danger_zone="long_context_1m")
    assert (caught.value.status, caught.value.code, caught.value.param) == (case["status"], case["code"], case["param"])
    assert not seen

@pytest.mark.parametrize("key", ["", " ", "\t"])
def test_1m_blank_keys(key):
    seen = []
    with SystemZero(api_key="SYNTHETIC", max_retries=0, transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, json={}))) as client:
        for extra in ({"idempotency_key": key}, {"headers": {"Idempotency-Key": key}}):
            with pytest.raises(InvalidRequestError) as caught:
                client.decide(REQUEST, danger_zone="long_context_1m", **extra)
            assert (caught.value.status, caught.value.code, caught.value.param) == (400, "invalid_request", "Idempotency-Key")
    assert not seen

@pytest.mark.parametrize("attachments", [None, []])
def test_1m_empty_attachments_and_metadata(attachments):
    seen = []
    with SystemZero(api_key="SYNTHETIC", max_retries=0, transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, json={"answers": {}}, headers={"sz-danger-zone": "long_context_1m"}))) as client:
        result = client.decide_with_response({**REQUEST, "attachments": attachments}, danger_zone="long_context_1m", idempotency_key="caller-key")
    assert seen[0].headers["idempotency-key"] == "caller-key"
    assert seen[0].headers["sz-danger-zone"] == "long_context_1m"
    assert result.metadata.danger_zone == ["long_context_1m"]

def test_1m_utf8_body_boundary():
    seen = []
    overhead = len(json.dumps({**REQUEST, "state": ""}, ensure_ascii=False, separators=(",", ":")).encode())
    state = "\u00e9" + "x" * (8_388_608 - overhead - 2)
    with SystemZero(api_key="SYNTHETIC", max_retries=0, transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, json={"answers": {}}))) as client:
        client.decide({**REQUEST, "state": state}, danger_zone="long_context_1m")
        with pytest.raises(InvalidRequestError) as caught:
            client.decide({**REQUEST, "state": state + "x"}, danger_zone="long_context_1m")
    assert (caught.value.status, caught.value.code) == (413, "payload_too_large")
    assert len(seen) == 1 and len(seen[0].content) == 8_388_608

def test_1m_stream_refusal_before_connect():
    import asyncio
    from system_zero import SystemZeroStreamClient
    with pytest.raises(LongContextNotAvailableError):
        SystemZeroStreamClient(api_key="SYNTHETIC", danger_zone="long_context_1m")
    client = SystemZeroStreamClient(api_key="SYNTHETIC")
    with pytest.raises(LongContextNotAvailableError):
        asyncio.run(client.start(REQUEST, danger_zone="long_context_1m"))
    assert client._runner is None

@pytest.mark.parametrize("workflow", [False, True])
def test_1m_cold_retry(workflow):
    from system_zero._retry import _retry_context
    seen = []
    def send(request):
        seen.append(request)
        return httpx.Response(503, json={"error": {"code": "service_unavailable"}}) if len(seen) == 1 else httpx.Response(200, json={"answers": {}})
    with SystemZero(api_key="SYNTHETIC", max_retries=1, sleep=lambda _: None, random=lambda: 0, transport=httpx.MockTransport(send)) as client:
        token = _retry_context.set({"workflow_recovery": workflow})
        try:
            client.decide(REQUEST, danger_zone="long_context_1m")
        finally:
            _retry_context.reset(token)
    assert len(seen) == 2
    assert seen[0].headers["idempotency-key"] == seen[1].headers["idempotency-key"]
    assert [request.headers["sz-danger-zone"] for request in seen] == ["long_context_1m", "long_context_1m"]

@pytest.mark.parametrize("status,code", [(402, "insufficient_credits"), (429, "long_context_daily_budget_exhausted")])
def test_1m_never_retries_credit_limits(status, code):
    seen = []
    with SystemZero(api_key="SYNTHETIC", sleep=lambda _: None, transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(status, json={"error": {"code": code, "retryable": True}}, headers={"x-should-retry": "true"}))) as client:
        with pytest.raises(Exception) as caught:
            client.decide(REQUEST, danger_zone="long_context_1m")
    assert (caught.value.status, caught.value.code) == (status, code)
    assert len(seen) == 1
