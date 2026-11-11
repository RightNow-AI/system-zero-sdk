"""SYNTHETIC regression for counted policy indicators and the closed public extension."""

from typing import Literal, get_type_hints

import httpx
import pytest

import system_zero as sdk


REQUEST = {
    "model": "system-zero", "state": "SYNTHETIC policy count.",
    "questions": {"ready": {"type": "boolean", "instructions": "Is it ready?"}},
}
CASES = [
    ("policy_max_cost_exceeded", 400, False, sdk.PolicyMaxCostExceededError),
    ("policy_confidence_failed", 422, True, sdk.PolicyConfidenceFailedError),
]
DETAILS = ["matching", "missing", "empty", "malformed", "null", "contradictory"]


def public_attribute_delta(error):
    baseline = sdk.InvalidRequestError("SYNTHETIC base failure.")
    return {name for name in dir(error) if not name.startswith("_")} - {
        name for name in dir(baseline) if not name.startswith("_")
    }


def details_for(kind, counted):
    
    return {
        "matching": {"counted": counted}, "missing": None, "empty": {},
        "malformed": {"counted": "unknown"}, "null": {"counted": None},
        "contradictory": {"counted": not counted},
    }[kind]


@pytest.mark.parametrize("code,status,counted,error_class", CASES, ids=[case[0] for case in CASES])
@pytest.mark.parametrize("kind", DETAILS)
def test_public_http_policy_error_keeps_only_the_counted_extension(code, status, counted, error_class, kind):
    details = details_for(kind, counted)
    body = {"error": {
        "message": "SYNTHETIC policy refusal.", "type": "invalid_request_error",
        "code": code, "retryable": False,
        **({} if details is None else {"details": details}),
    }}
    with sdk.SystemZero(api_key="SYNTHETIC", max_retries=0,
                        transport=httpx.MockTransport(lambda _request: httpx.Response(status, json=body))) as client:
        with pytest.raises(error_class) as caught:
            client.decide(REQUEST)
    assert caught.value.counted is counted
    assert public_attribute_delta(caught.value) == {"counted"}
    assert caught.value.details == details


@pytest.mark.parametrize("code,status,counted,error_class", CASES, ids=[case[0] for case in CASES])
@pytest.mark.parametrize("kind", DETAILS)
def test_public_specialization_keeps_only_the_counted_extension(code, status, counted, error_class, kind):
    details = details_for(kind, counted)
    source = sdk.SystemZeroError("SYNTHETIC policy refusal.", status=status,
                                type="invalid_request_error", code=code, details=details)
    error = sdk.as_policy_error(source)
    assert isinstance(error, error_class)
    assert error.counted is counted
    assert public_attribute_delta(error) == {"counted"}
    assert error.details is details
    assert sdk.as_policy_error(error) is error


def test_public_counted_types_keep_the_existing_per_code_boolean_literals():
    assert get_type_hints(sdk.PolicyMaxCostExceededError)["counted"] == Literal[False]
    assert get_type_hints(sdk.PolicyConfidenceFailedError)["counted"] == Literal[True]
