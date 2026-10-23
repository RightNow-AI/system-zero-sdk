"""Public tool schemas, packaged limits and injected clients work without a service."""

import json
from pathlib import Path
from types import SimpleNamespace

import system_zero.tools as tools


def configured():
    calls = []

    def decide(request, **_options):
        calls.append(request)
        return SimpleNamespace(data={"answers": {}}, metadata=SimpleNamespace(request_id="req_public"))

    client = SimpleNamespace(decide_with_response=decide)
    return tools.create_tool_handler(
        client, max_decide_calls=10,
    ), calls


def test_provider_schema_normalization_and_invalid_argument_refusal():
    handler, calls = configured()
    arguments = {
        "state": {"json": '{"status":"ready"}'},
        "questions": [{"name": "ready", "type": "boolean", "instructions": "Is the status ready?"}],
    }
    assert handler("system_zero_decide", arguments)["status"] == "ok"
    assert calls == [{
        "model": "system-zero", "state": {"status": "ready"},
        "questions": {"ready": {"type": "boolean", "instructions": "Is the status ready?"}},
    }]
    assert handler("system_zero_list_models", {"extra": True})["error"]["code"] == "invalid_request"
    assert handler("unknown_tool", {})["error"]["code"] == "invalid_request"
    assert len(calls) == 1
    names = [tool["function"]["name"] for tool in tools.NESTED_FUNCTION_TOOLS]
    assert names == [tool["name"] for tool in tools.INPUT_SCHEMA_TOOLS]
    assert all(tool["function"]["strict"] for tool in tools.NESTED_FUNCTION_TOOLS)


def test_packaged_argument_limits_and_real_document_depth():
    limits = json.loads(Path(tools.__file__).with_name("argument-limits.json").read_text(encoding="utf-8"))
    assert limits == {"maxBytes": 24 * 1024 * 1024, "maxDepth": 256, "maxValues": 2_000_000}
    handler, calls = configured()
    document = "[" * limits["maxDepth"] + '"x"' + "]" * limits["maxDepth"]
    assert handler("system_zero_decide", {"state": {"json": document}, "questions": []})["status"] == "ok"
    refused = handler("system_zero_decide", {"state": {"json": "[" + document + "]"}, "questions": []})
    assert refused["error"]["code"] == "invalid_request"
    assert len(calls) == 1


def test_models_handler_uses_injected_client_without_spending_a_decide_budget():
    calls = []

    def models():
        calls.append("models")
        return SimpleNamespace(
            data={"object": "list", "data": []}, metadata=SimpleNamespace(request_id="req_public"),
        )

    client = SimpleNamespace(models=SimpleNamespace(list_with_response=models))
    handler = tools.create_tool_handler(client)
    assert handler("system_zero_list_models", {}) == {
        "object": "list", "data": [], "status": "ok", "request_id": "req_public",
    }
    assert handler("system_zero_list_models", {"extra": True})["error"]["code"] == "invalid_request"
    assert calls == ["models"]
    assert handler.budget == {"decide_calls": 0}
