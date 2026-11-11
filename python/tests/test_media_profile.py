"""Published media uses synthetic signature bytes and injected transports."""
import base64
import json
import struct
from pathlib import Path

import httpx
import pytest
import system_zero as sdk
from system_zero.tools import create_tool_handler

REQUEST = {"model": "system-zero", "state": "Synthetic media", "questions": {"ready": {"type": "boolean", "instructions": "Ready?"}}}
PNG = bytes.fromhex("89504e470d0a1a0a")

def url(mime, data):
    return "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")

IMAGE = {"name": "picture", "type": "image", "data": url("image/png", PNG)}
def wav(codec=1, channels=1, rate=16000, bits=16, frames=160):
    align = channels * ((bits + 7) // 8)
    data = bytes(frames * align)
    return b"RIFF" + struct.pack("<I", 36 + len(data) + len(data) % 2) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, codec, channels, rate, rate * align, align, bits) + b"data" + struct.pack("<I", len(data)) + data + bytes(len(data) % 2)

AUDIO = {"name": "recording", "type": "audio", "data": url("audio/wav", wav())}

def test_selected_registry_and_version():
    assert sdk.VERSION == sdk.__version__ == "0.4.6"
    assert dict(sdk.MIMES) == {"image": ("image/jpeg", "image/png"), "audio": ("audio/wav",), "document": ("application/pdf",), "video": ("video/mp4",)}
    assert sdk.MEDIA_LIMITS["max_audio_duration_ms"] == 60_000
    spec = json.loads((Path(__file__).resolve().parents[2] / "openapi.json").read_text())
    assert spec["components"]["schemas"]["Attachment"]["properties"]["type"]["enum"] == ["image", "audio", "document", "video"]

@pytest.mark.parametrize("kind,mime,raw", (
    ("audio", "audio/wav", wav()),
    ("audio", "audio/wav", wav(3, 2, 16000, 32)),
    ("document", "application/pdf", b"%PDF-1.7\nSynthetic signature fixture"),
    ("video", "video/mp4", bytes.fromhex("000000186674797069736f6d")),
))
def test_media_helpers_preflight_and_ordinary_http(kind, mime, raw):
    attachment = sdk.inline_attachment(name="input", type=kind, mime=mime, data=raw)
    assert attachment == {"name": "input", "type": kind, "data": url(mime, raw)}
    assert sdk.validate_attachments([attachment]) == [attachment]
    assert sdk.attachment_magic_matches(mime, raw)
    seen = []
    with sdk.SystemZero(api_key="SYNTHETIC", transport=httpx.MockTransport(
            lambda request: (seen.append(json.loads(request.content)), httpx.Response(200, json={"answers": {}}))[1])) as client:
        client.decide({**REQUEST, "attachments": [attachment]})
    assert seen == [{**REQUEST, "attachments": [attachment]}]

@pytest.mark.parametrize("extra", ({}, {"attachments": None}, {"attachments": [IMAGE]}))
def test_optional_attachments_keep_the_wire_body(extra):
    seen = []
    body = {**REQUEST, **extra}
    with sdk.SystemZero(api_key="SYNTHETIC", transport=httpx.MockTransport(
            lambda request: (seen.append(json.loads(request.content)), httpx.Response(200, json={"answers": {}}))[1])) as client:
        client.decide(body)
    assert seen == [body]

def test_pdf_format_and_image_count_checks():
    with pytest.raises(sdk.SystemZeroError):
        sdk.inline_attachment(name="paper", type="document", mime="text/plain", data=b"text")
    with pytest.raises(sdk.SystemZeroError) as caught:
        sdk.validate_attachments([{**IMAGE, "name": "image" + str(i)} for i in range(5)])
    assert caught.value.code == "media_unsupported"

@pytest.mark.parametrize("raw", (wav(rate=44100), b"RIFF malformed"))
def test_audio_rate_and_structure(raw):
    with pytest.raises(sdk.SystemZeroError) as caught:
        sdk.inline_attachment(name="recording", type="audio", mime="audio/wav", data=raw)
    assert caught.value.code == "media_unsupported"

def test_audio_count():
    with pytest.raises(sdk.SystemZeroError) as caught:
        sdk.validate_attachments([AUDIO, {**AUDIO, "name": "second"}])
    assert caught.value.code == "media_unsupported"

@pytest.mark.parametrize("channels", (1, 2))
def test_audio_duration_boundary_is_inclusive(channels):
    raw = wav(channels=channels, bits=8, frames=960_000)
    attachment = sdk.inline_attachment(name="recording", type="audio", mime="audio/wav", data=raw)
    assert attachment["data"] == url("audio/wav", raw)
    assert sdk.validate_attachments([attachment]) == [attachment]


@pytest.mark.parametrize("frames", (960_001, 976_000))
def test_audio_over_duration_matches_api_refusal(frames):
    raw = wav(bits=8, frames=frames)
    for preflight in (
        lambda: sdk.inline_attachment(name="recording", type="audio", mime="audio/wav", data=raw),
        lambda: sdk.validate_attachments([{"name": "recording", "type": "audio", "data": url("audio/wav", raw)}]),
    ):
        with pytest.raises(sdk.SystemZeroError) as caught:
            preflight()
        assert (caught.value.status, caught.value.code, caught.value.param) == (413, "media_too_large", "attachments")


def test_audio_bytes_are_independently_bounded():
    raw = wav(channels=4, frames=400_000)
    with pytest.raises(sdk.SystemZeroError) as caught:
        sdk.inline_attachment(name="recording", type="audio", mime="audio/wav", data=raw)
    assert caught.value.code == "media_too_large"

def test_audio_answer_stays_uncalibrated():
    reply = {"answers": {"ready": {"type": "boolean", "decision": True, "calibrated": False}}}
    with sdk.SystemZero(api_key="SYNTHETIC", transport=httpx.MockTransport(lambda request: httpx.Response(200, json=reply))) as client:
        assert client.decide({**REQUEST, "attachments": [AUDIO]}) == reply

@pytest.mark.parametrize("code", ("audio_not_available", "video_not_available", "document_not_available"))
def test_api_holds_preserve_status_and_code_without_retry(code):
    seen = []
    def send(request):
        seen.append(request)
        return httpx.Response(404, json={"error": {"code": code, "message": "Synthetic hold.", "type": "not_found_error", "retryable": False}})
    with sdk.SystemZero(api_key="SYNTHETIC", transport=httpx.MockTransport(send)) as client:
        with pytest.raises(sdk.SystemZeroError) as caught:
            client.decide(REQUEST)
    assert caught.value.status == 404 and caught.value.code == code
    assert len(seen) == 1

@pytest.mark.parametrize("attachment", (
    IMAGE,
    AUDIO,
    {"name": "paper", "type": "document", "data": url("application/pdf", b"%PDF-1.7\nSynthetic")},
    {"name": "clip", "type": "video", "data": url("video/mp4", bytes.fromhex("000000186674797069736f6d"))},
))
def test_function_tools_carry_published_inline_kinds(attachment):
    seen = []
    with sdk.SystemZero(api_key="SYNTHETIC", transport=httpx.MockTransport(
            lambda request: (seen.append(json.loads(request.content)), httpx.Response(200, json={"answers": {}}))[1])) as client:
        result = create_tool_handler(client)("system_zero_decide", {"state": "Synthetic", "questions": [{"name": "ready", "type": "boolean", "instructions": "Ready?", "criteria": None}], "model": None, "attachments": [attachment]})
    assert result["status"] == "ok"
    assert seen[0]["attachments"] == [attachment]

@pytest.mark.parametrize("kind", ["image", "document", "video"])
def test_audio_mixed_with_another_kind_uses_api_error(kind):
    other = {"name": "other", "type": kind, "data": url("application/pdf" if kind == "document" else "video/mp4" if kind == "video" else "image/png", PNG)}
    for attachments in ([AUDIO, other], [other, AUDIO]):
        with pytest.raises(sdk.AudioNotAvailableError) as caught:
            sdk.validate_attachments(attachments)
        assert (caught.value.status, caught.value.code, caught.value.param) == (404, "audio_not_available", "attachments")
