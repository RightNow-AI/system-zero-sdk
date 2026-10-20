"""SDK call bounds remain independent of the retained cost bounds."""

from pathlib import Path
import tempfile

import httpx
import pytest

from system_zero import (
    InvalidRequestError,  SystemZero, SystemZeroError,
    run_batch, run_flow,
)


REQUEST = {"model": "system-zero", "state": "SYNTHETIC state",
           "questions": {"ready": {"type": "boolean", "instructions": "Ready?"}}}
ANSWER = {"type": "boolean", "probability": 0.8, "decision": True, "confidence": 0.8, "calibrated": False,
          "alternatives": [{"decision": False, "confidence": 0.2}], "calibration_version": "synthetic"}
REPLY = {"id": "szd_synthetic", "model": "system-zero", "served_by": "synthetic", "created": 1,
         "context": {"route": "direct", "tokens": 1}, "answers": {"ready": ANSWER},
         "usage": {"input_tokens": 1, "cached_input_tokens": 0, "media_tokens": 0,
                   "long_input_tokens": 0, "media_items": 0, "thought_tokens": 0}}
ITEMS = [{"id": name, "request": REQUEST, "expected": {"ready": True}} for name in ("one", "two", "three")]
FLOW = {"version": 1, "request": {"model": REQUEST["model"], "state": REQUEST["state"]},
        "steps": [{"id": name, "questions": REQUEST["questions"],
                   "next": {"rules": [], "default": "two" if index == 0 else None}}
                  for index, name in enumerate(("one", "two"))]}


@pytest.fixture
def receipt_path():
    scratch = Path.cwd() / "tmp"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="call-limit-", dir=scratch) as directory:
        yield Path(directory) / "receipts.jsonl"


def client(seen):
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=REPLY)
    return SystemZero("SYNTHETIC", transport=httpx.MockTransport(respond), timeout=0.025, max_retries=0)


def test_zero_count_batch_stops_before_dispatch_with_cost_budget_available(receipt_path):
    seen = []
    with client(seen) as sdk:
        result = run_batch(sdk, ITEMS, run_id="zero-calls", results_path=receipt_path,
                            max_decide_calls=0, deadline_ms=10000, concurrency=2)
    assert result == {"records": [], "stopped": "call_limit", "decide_calls": 0}
    assert not seen


def test_count_is_shared_by_workers_and_durable_admissions_on_resume(receipt_path):
    seen = []
    options = {"run_id": "count-resume", "results_path": receipt_path,
               "max_decide_calls": 1, "deadline_ms": 10000, "concurrency": 2}
    with client(seen) as sdk:
        first = run_batch(sdk, ITEMS, **options)
        assert first["stopped"] == "call_limit"
        assert first["decide_calls"] == 1
        assert [row["id"] for row in first["records"]] == ["one"]
        assert [row["outcome"] for row in first["records"]] == ["completed"]
        assert len(seen) == 1
        with pytest.raises(InvalidRequestError) as caught:
            run_batch(sdk, ITEMS, **{**options, "max_decide_calls": 0})
        assert caught.value.param == "max_decide_calls"
        assert len(seen) == 1
        resumed = run_batch(sdk, ITEMS, **{**options, "max_decide_calls": 3})
    assert resumed["stopped"] is None
    assert resumed["decide_calls"] == 3
    assert [row["id"] for row in resumed["records"]] == ["one", "two", "three"]
    assert all(row["outcome"] == "completed" for row in resumed["records"])
    assert len(seen) == 3


@pytest.mark.parametrize("value", (True, -1, 0.5, float("nan"), float("inf"), 2**53))
def test_invalid_optional_batch_count_is_refused_before_dispatch(value, receipt_path):
    seen = []
    with client(seen) as sdk, pytest.raises(InvalidRequestError) as caught:
        run_batch(sdk, [], run_id="invalid-count", results_path=receipt_path,
                  max_decide_calls=value, deadline_ms=10000)
    assert caught.value.code == "invalid_request"
    assert caught.value.param == "max_decide_calls"
    assert caught.value.message == "The batch configuration or receipt is invalid."
    assert not seen


@pytest.mark.parametrize("count", (0, 1))
def test_flow_count_cap_is_admitted_before_dispatch_and_is_reported(count):
    seen = []
    with client(seen) as sdk, pytest.raises(SystemZeroError) as caught:
        run_flow(sdk, FLOW, run_id="counted-flow", max_decide_calls=count,
                  deadline_ms=10000, now=lambda: 0)
    error = caught.value
    assert error.code == "call_limit_reached"
    assert error.retryable is False
    assert error.trace["decide_calls"] == count
    assert "reserved_cost_microcents" not in error.trace
    assert [step["decide_calls"] for step in error.trace["steps"]] == ([0] if count == 0 else [1, 0])
    assert error.trace["steps"][-1]["error"]["code"] == "call_limit_reached"
    assert len(seen) == count


@pytest.mark.parametrize("value", (True, -1, 0.5, float("nan"), float("inf"), 2**53, 33))
def test_invalid_optional_flow_count_is_refused_before_dispatch(value):
    seen = []
    with client(seen) as sdk, pytest.raises(InvalidRequestError) as caught:
        run_flow(sdk, FLOW, run_id="invalid-flow-count", max_decide_calls=value,
                  deadline_ms=10000, now=lambda: 0)
    assert caught.value.code == "invalid_request"
    if value == 33:
        assert "32" in caught.value.message and "max_decide_calls" in caught.value.message
    assert not seen


def test_default_batch_stops_at_1000_durable_admissions(receipt_path):
    seen = []
    many = [{**ITEMS[0], "id": "item-%d" % index} for index in range(1001)]
    options = dict(run_id="default-batch", results_path=receipt_path,
                   deadline_ms=120000, concurrency=2)
    with client(seen) as sdk:
        result = run_batch(sdk, many, **options)
        assert result["stopped"] == "call_limit"
        assert result["decide_calls"] == len(result["records"]) == len(seen) == 1000
        resumed = run_batch(sdk, many, **options)
        assert resumed["decide_calls"] == len(seen) == 1000
        assert resumed["stopped"] == "call_limit"
        extended = run_batch(sdk, many, **options, max_decide_calls=1001)
        assert extended["stopped"] is None
        assert extended["decide_calls"] == len(seen) == 1001


def steps(count):
    return [{"id": "s%d" % index, "questions": REQUEST["questions"],
             "next": {"rules": [], "default": "s%d" % (index + 1) if index + 1 < count else None}}
            for index in range(count)]


def test_omitted_flow_cap_always_traces_all_32_calls():
    seen = []
    with client(seen) as sdk:
        result = run_flow(sdk, {**FLOW, "steps": steps(32)}, run_id="default-flow",

                          deadline_ms=10000, now=lambda: 0)
    assert result["decide_calls"] == len(seen) == 32
    assert [step["decide_calls"] for step in result["steps"]] == [1] * 32


def test_33_step_flow_is_refused_before_dispatch():
    seen = []
    with client(seen) as sdk, pytest.raises(InvalidRequestError):
        run_flow(sdk, {**FLOW, "steps": steps(33)}, run_id="too-many",
                  deadline_ms=10000)
    assert not seen
