"""Client configuration and local admission use synthetic transports only."""

import httpx
import pytest
from urllib.parse import urlsplit

from system_zero import SystemZero
from system_zero._retry import _recover_workflow, _retry_response

from system_zero.tools import ToolHandler, handle_tool_call


TOOL_ARGS = {"state": {"json": '{"ready":true}'}, "questions": [
    {"name": "ready", "type": "boolean", "instructions": "Is this ready?"},
]}
CONFIGURATION_ERROR = "api_key must contain only visible ASCII characters."
LOOPBACK_HOSTS = (".".join(map(str, (127, 0, 0, 1))), "[" + ":" + ":1]", "".join(("local", "host")))


@pytest.mark.parametrize("character", ("\n", "\r", "\0", "\t", " ", "\x7f", "\u00e9"))
def test_malformed_key_is_refused_before_http_client_construction(character, monkeypatch):
    calls = []
    key = "SYNTHETIC" + character + "VALUE"
    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: calls.append(True))
    with pytest.raises(ValueError) as caught:
        SystemZero(api_key=key)
    assert str(caught.value) == CONFIGURATION_ERROR
    assert caught.value.__cause__ is None
    assert key not in repr(caught.value)
    assert calls == []


def test_environment_key_gets_the_same_fixed_refusal(monkeypatch):
    monkeypatch.setenv("SYSTEM_ZERO_API_KEY", "SYNTHETIC\nVALUE")
    with pytest.raises(ValueError, match="^api_key must contain only visible ASCII characters\\.$"):
        SystemZero()


@pytest.mark.parametrize("base_url", (
    "https://sdk.example.com/v1?", "https://sdk.example.com/v1#",
    "https://sdk.example.com/v1?x=1", "https://sdk.example.com/v1#part",
    urlsplit("https://sdk.example.com/v1")._replace(netloc="user:pass@sdk.example.com").geturl(),
    "http://sdk.example.com/v1",
))
def test_invalid_url_is_refused_by_client_and_tool_factory(base_url):
    with pytest.raises(ValueError):
        SystemZero(api_key="SYNTHETIC", base_url=base_url)
    with pytest.raises((ValueError, TypeError)):
        ToolHandler(base_url=base_url)


@pytest.mark.parametrize("base_url", ("https://sdk.example.com/v1",) + tuple(
    urlsplit("http://sdk.example.com/v1")._replace(netloc=host + ":8000").geturl() for host in LOOPBACK_HOSTS
))
def test_secure_or_loopback_base_is_accepted_without_dispatch(base_url):
    calls = []
    with SystemZero(api_key="SYNTHETIC", base_url=base_url,
                    transport=httpx.MockTransport(lambda request: calls.append(request))) as client:
        assert client.base_url
    with ToolHandler(base_url=base_url):
        pass
    assert calls == []


def test_lazy_client_configuration_failure_does_not_count_a_call(monkeypatch):
    monkeypatch.delenv("SYSTEM_ZERO_API_KEY", raising=False)
    with ToolHandler(max_decide_calls=2) as handler:
        for _index in range(2):
            assert handler("system_zero_decide", TOOL_ARGS)["status"] == "error"
            assert handler.budget == {"decide_calls": 0}


def test_module_level_tool_helper_admits_zero_decide_calls(monkeypatch):
    monkeypatch.delenv("SYSTEM_ZERO_API_KEY", raising=False)
    assert handle_tool_call("system_zero_decide", TOOL_ARGS) == {
        "status": "error", "request_id": None,
        "error": {"code": "call_limit_reached", "message": "The local call limit has been reached."},
    }


def test_explicit_refusal_before_work_recovery_keeps_ambiguous_replies_terminal():
    headers = {"x-should-retry": "true"}
    for code, cause, expected in (
        ("rate_limit_exceeded", "key_rate", True), ("rate_limit_exceeded", "preauth", True),
        ("concurrency_limit_exceeded", "account_concurrency", True),
        ("rate_limit_exceeded", "capacity", False), ("capacity_exhausted", "capacity", False),
        ("invalid_api_key", "key_rate", False),
    ):
        assert _recover_workflow(429, code, headers, True, {"cause": cause}) is expected
        assert _recover_workflow(429, code, headers, False, {"cause": cause}) is False
    assert _retry_response(503, "service_unavailable", headers, True) is True
    assert _retry_response(429, "rate_limit_exceeded", headers, True) is True
    assert _retry_response(401, "invalid_api_key", headers, True) is False
