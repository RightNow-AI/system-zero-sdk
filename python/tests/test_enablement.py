import httpx
import pytest
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

def test_batch_forwards_and_validates_option(tmp_path):
    seen = []
    def send(request):
        seen.append(request)
        return httpx.Response(404, json={"error": {"code": "long_context_not_available", "type": "not_found_error", "message": "This route is not available."}})
    with SystemZero(api_key="synthetic", timeout=1, max_retries=0, transport=httpx.MockTransport(send)) as client:
        items = [{"id": "one", "request": REQUEST, "expected": {"q": True}}]
        options = dict(run_id="enabled", results_path=tmp_path / "rows.jsonl", deadline_ms=10000, concurrency=1)
        result = run_batch(client, items, danger_zone="accept", **options)
        assert result["records"][0]["outcome"] == "held"
        assert seen[0].headers["SZ-Danger-Zone"] == "accept"
        with pytest.raises(InvalidRequestError):
            run_batch(client, items, danger_zone="wrong", **options)
    assert len(seen) == 1

def test_flow_forwards_option():
    seen = []
    def send(request):
        seen.append(request)
        return httpx.Response(404, json={"error": {"code": "long_context_not_available", "type": "not_found_error", "message": "This route is not available."}})
    flow = {"version": 1, "request": {"model": REQUEST["model"], "state": REQUEST["state"]}, "steps": [{"id": "one", "questions": REQUEST["questions"], "next": {"rules": [], "default": None}}]}
    with SystemZero(api_key="synthetic", timeout=1, max_retries=0, transport=httpx.MockTransport(send)) as client:
        with pytest.raises(LongContextNotAvailableError):
            run_flow(client, flow, run_id="enabled", deadline_ms=10000, danger_zone="accept")
    assert seen[0].headers["SZ-Danger-Zone"] == "accept"

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
