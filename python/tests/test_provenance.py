"""SYNTHETIC provenance fixtures shared with TypeScript; no external service."""

from stream_limit_fixture import TEST_STREAM_LIMITS

import asyncio
import json
from pathlib import Path
from typing import get_args, get_type_hints
from urllib.parse import urlsplit

import httpx
import pytest
from websockets.asyncio.server import serve

import system_zero as sdk
from system_zero import types
from system_zero import (
    DecideReply, DecisionProvenance, ProvenanceAttachmentClass, ProvenanceReplyFields,
    ProvenanceRoute, StreamDecision, SystemZero, SystemZeroStreamClient, decisions_for, provenance_for,
)


FIXTURES = json.loads((Path(__file__).resolve().parents[2] / "fixtures" / "provenance" / "cases.json").read_text(encoding="utf-8"))
REQUEST = {"model": "system-zero", "state": "SYNTHETIC",
           "questions": {"ready": {"type": "boolean", "instructions": "Ready?"}}}
PROTOCOL = "system-zero.stream.v1"
LOOPBACK = ".".join(map(str, (127, 0, 0, 1)))


def test_package_root_exports_the_provenance_contract():
    names = {
        "DecisionProvenance", "ProvenanceAttachmentClass", "ProvenanceReplyFields",
        "ProvenanceRoute", "provenance_for",
    }
    assert names <= set(sdk.__all__)
    assert all(getattr(sdk, name) is globals()[name] for name in names)
    assert types.DecisionProvenance is DecisionProvenance


@pytest.mark.parametrize("reply_type", [
    DecideReply,
    get_type_hints(SystemZero.decide)["return"],
    get_args(get_type_hints(SystemZero.decide_with_response)["return"])[0],
    get_type_hints(StreamDecision)["reply"],
], ids=["public-reply", "http", "http-with-response", "stream"])
def test_actual_public_reply_annotations_keep_provenance_optional(reply_type):
    variants = get_args(reply_type)
    assert {variant.__name__ for variant in variants} == {"V1DecideReply", "LegacyDecideReply"}
    for variant in variants:
        hints = get_type_hints(variant)
        assert hints["provenance"] is DecisionProvenance
        assert variant.__optional_keys__ == frozenset({"provenance"} | ({"policy"} if "policy" in hints else set()))
        if "policy" in hints:
            assert hints["policy"] is sdk.Policy
        assert variant.__required_keys__ == frozenset({
            "id", "model", "served_by", "created", "context", "usage", "answers",
        })


def test_provenance_types_match_wire_fields_and_keep_evidence_open():
    annotations = get_type_hints(DecisionProvenance)
    assert set(annotations) == set(FIXTURES[0]["reply"]["provenance"])
    assert DecisionProvenance.__required_keys__ == frozenset(annotations)
    assert annotations["numeric_fingerprint"] is str
    assert annotations["created"] is int
    assert annotations["keyed_replay"] is bool
    assert get_args(annotations["calibration_version"]) == (str, str)
    assert set(get_args(ProvenanceAttachmentClass)) == {"document", "image", "audio", "video"}
    assert set(get_args(ProvenanceRoute)) == {"direct", "long"}
    assert ProvenanceReplyFields.__optional_keys__ == frozenset({"provenance"})
    assert "evidence" not in annotations


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda item: item["name"])
def test_accessor_preserves_supplied_metadata_without_filling_an_absence(fixture):
    reply = fixture["reply"]
    before = json.dumps(reply)
    provenance = provenance_for(reply)
    assert provenance is reply.get("provenance")
    assert json.dumps(reply) == before
    assert decisions_for(reply) == decisions_for({key: value for key, value in reply.items() if key != "provenance"})
    if provenance is None:
        assert "provenance" not in reply
        return
    assert "evidence" not in provenance
    assert provenance["decision_id"] == reply["id"]
    assert provenance["resolved_release"] == reply["model"]
    assert provenance["route"] == reply["context"]["route"]
    assert provenance["served_by"] == reply["served_by"]
    assert provenance["calibration_version"] == {
        name: answer["calibration_version"] for name, answer in reply["answers"].items()
        if "error" not in answer
    }
    assert provenance["attachment_classes"] == sorted(set(provenance["attachment_classes"]))


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda item: item["name"])
def test_http_preserves_wire_provenance_and_original_request_identity(fixture):
    wire = json.dumps(fixture["reply"], separators=(",", ":"))
    headers = {"content-type": "application/json", "x-request-id": fixture["request_id"]}
    if fixture["replayed"]:
        headers["x-rightnow-idempotent-replay"] = "true"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=wire, headers=headers))
    with SystemZero(api_key="rnsk_synthetic", base_url="https://sdk.example.com/v1", max_retries=0, transport=transport) as client:
        result = client.decide_with_response(REQUEST)
    assert result.data == fixture["reply"]
    assert json.dumps(result.data, separators=(",", ":")) == wire
    assert result.metadata.request_id == fixture["request_id"]
    assert result.metadata.replayed is fixture["replayed"]
    assert provenance_for(result.data) is result.data.get("provenance")
    assert provenance_for(result.data) == fixture["reply"].get("provenance")
    if fixture["reply"].get("provenance", {}).get("keyed_replay"):
        assert provenance_for(result.data)["request_id"] != result.metadata.request_id


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda item: item["name"])
def test_stream_preserves_the_same_complete_reply(fixture):
    async def run():
        async def handler(socket):
            stream_id = "szws_" + "1" * 32
            await socket.send(json.dumps({
                "type": "hello", "stream_id": stream_id, "seq": 1, "protocol": PROTOCOL,
                "request_id": "req_upgrade", "rate_limit": None,
                "limits": {**TEST_STREAM_LIMITS, "heartbeat_interval_ms": 30000, "max_duration_ms": 3600000},
            }))
            async for raw in socket:
                frame = json.loads(raw)
                if frame["type"] != "start":
                    continue
                await socket.send(json.dumps({
                    "type": "ack", "stream_id": stream_id, "seq": 2,
                    "event_id": frame["event_id"], "state_version": 1, "state_bytes": 11,
                    "synthesized_body_bytes": 100, "scheduled": True,
                }))
                await socket.send(json.dumps({
                    "type": "decision", "stream_id": stream_id, "seq": 3,
                    "state_version": 1, "covered_event_ids": [frame["event_id"]],
                    "reply": fixture["reply"], "request_id": fixture["request_id"],
                    "http_status": 200, "server_timing": None, "latency_ms": None,
                    "rate_limit": None, "queue_ms": 0, "decide_ms": 1,
                }))

        async with serve(handler, LOOPBACK, 0, subprotocols=[PROTOCOL]) as server:
            port = server.sockets[0].getsockname()[1]
            stream_url = urlsplit("ws://sdk.example.com/v1/system-zero/stream")._replace(netloc=f"{LOOPBACK}:{port}").geturl()
            async with SystemZeroStreamClient(api_key="rnsk_synthetic", reconnect_policy="never",
                                               stream_url=stream_url) as client:
                ack = await client.start(REQUEST)
                result = await asyncio.wait_for(client.wait_for_decision(ack), 3)
                assert result.decision["reply"] == fixture["reply"]
                assert result.decision["request_id"] == fixture["request_id"]
                assert provenance_for(result.decision["reply"]) is result.decision["reply"].get("provenance")
                assert provenance_for(result.decision["reply"]) == fixture["reply"].get("provenance")
                assert json.dumps(result.decision["reply"]) == json.dumps(fixture["reply"])

    asyncio.run(run())
