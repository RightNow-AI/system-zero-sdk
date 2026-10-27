"""Served media uses synthetic bytes and injected transports."""
import base64
import json
from pathlib import Path
import struct

import httpx
import pytest
import system_zero as sdk
from system_zero.tools import create_tool_handler

REQUEST = {"model": "system-zero", "state": "Synthetic media", "questions": {"ready": {"type": "boolean", "instructions": "Ready?"}}}
PNG = bytes([137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82, 0, 0, 0, 1, 0, 0, 0, 1, 8, 2, 0, 0, 0, 144, 119, 83, 222, 0, 0, 0, 12, 73, 68, 65, 84, 120, 156, 99, 96, 96, 96, 0, 0, 0, 4, 0, 1, 246, 23, 56, 85, 0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130])


def wav(rate=16000, codec=1, channels=1, bits=16):
    align = channels * ((bits + 7) // 8)
    content = b"WAVEfmt " + struct.pack("<IHHIIHH", 16, codec, channels, rate, rate * align, align, bits)
    content += b"data" + struct.pack("<I", align) + bytes(align + align % 2)
    return b"RIFF" + struct.pack("<I", len(content)) + content


def url(mime, data):
    return "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")


IMAGE = {"name": "picture", "type": "image", "data": url("image/png", PNG)}
AUDIO = {"name": "sound", "type": "audio", "data": url("audio/wav", wav())}


def test_pdf_helper_and_ordinary_request():
    raw = b"%PDF-1.7\nSynthetic signature fixture"
    attachment = sdk.inline_attachment(name="paper", type="document", mime="application/pdf", data=raw)
    assert attachment == {"name": "paper", "type": "document", "data": url("application/pdf", raw)}
    with pytest.raises(sdk.SystemZeroError):
        sdk.inline_attachment(name="paper", type="document", mime="text/plain", data=raw)
    seen = []
    with sdk.SystemZero(api_key="SYNTHETIC", transport=httpx.MockTransport(
            lambda request: (seen.append(json.loads(request.content)), httpx.Response(200, json={"answers": {}}))[1])) as client:
        client.decide({**REQUEST, "attachments": [attachment]})
    assert seen == [{**REQUEST, "attachments": [attachment]}]


def test_served_registry_limits_and_version():
    assert sdk.VERSION == sdk.__version__ == "0.4.1"
    assert dict(sdk.MIMES) == {"image": ("image/png", "image/jpeg"), "audio": ("audio/wav",), "document": ("application/pdf",), "video": ("video/mp4",)}
    assert dict(sdk.MEDIA_LIMITS) == {"max_attachments": 16, "max_images": 4, "max_audio": 1, "max_inline_bytes": 2621440,
                                    "max_body_bytes": 3665920, "audio_sample_rate": 16000, "max_audio_duration_ms": 300000, "max_audio_channels": 64, "max_image_pixels": 16777216, "max_image_aspect_ratio": 50}
    spec = json.loads((Path(__file__).resolve().parents[2] / "openapi.json").read_text())
    assert spec["components"]["schemas"]["Attachment"]["properties"]["type"]["enum"] == ["image", "audio", "document", "video"]


@pytest.mark.parametrize("extra", ({}, {"attachments": None}, {"attachments": [IMAGE]}, {"attachments": [AUDIO]}, {"attachments": [IMAGE, AUDIO]}))
def test_wire_preserves_text_null_image_and_audio(extra):
    seen = []
    body = {**REQUEST, **extra}
    with sdk.SystemZero(api_key="SYNTHETIC", max_retries=0, transport=httpx.MockTransport(
            lambda request: (seen.append(json.loads(request.content)), httpx.Response(200, json={"answers": {}}))[1])) as client:
        client.decide(body)
    assert seen == [body]


@pytest.mark.parametrize("rate,codec,channels,bits", ((16000, 1, 1, 8), (16000, 1, 2, 16), (16000, 3, 2, 32), (16000, 3, 64, 64)))
def test_wav_pcm_float_multichannel(rate, codec, channels, bits):
    raw = wav(rate, codec, channels, bits)
    assert sdk.inline_attachment(name="sound", type="audio", mime="audio/wav", data=raw) == {"name": "sound", "type": "audio", "data": url("audio/wav", raw)}


@pytest.mark.parametrize("mime,raw", (("image/png", PNG), ("image/jpeg", bytes.fromhex("ffd8ff"))))
def test_image_signatures(mime, raw):
    assert sdk.attachment_magic_matches(mime, raw)
    assert sdk.attachment_magic_matches("application/pdf", b"%PDF-")
    assert sdk.inline_attachment(name="picture", type="image", mime=mime, data=raw)["data"] == url(mime, raw)


@pytest.mark.parametrize("code,action", [
    ("invalid_request", lambda: sdk.inline_attachment(name="9bad", type="image", mime="image/png", data=PNG)),
    ("media_unsupported", lambda: sdk.inline_attachment(name="x", type="image", mime="image/bmp", data=PNG)),
    ("media_decode_failed", lambda: sdk.inline_attachment(name="x", type="image", mime="image/png", data=b"bad")),
    ("media_unpriced", lambda: sdk.inline_attachment(name="x", type="image", mime="image/gif", data=b"GIF89a")),
    ("media_unpriced", lambda: sdk.inline_attachment(name="x", type="image", mime="image/webp", data=b"RIFF0000WEBP")),
    ("media_too_large", lambda: sdk.inline_attachment(name="x", type="image", mime="image/png", data=bytes(2621441))),
    *[("media_unsupported", lambda rate=rate: sdk.inline_attachment(name="x", type="audio", mime="audio/wav", data=wav(rate))) for rate in (8000, 22050, 44100, 48000)],
    *[("media_unsupported", lambda mime=mime: sdk.inline_attachment(name="x", type="audio", mime=mime, data=wav())) for mime in ("audio/mpeg", "audio/flac", "audio/ogg", "audio/mp4", "audio/x-wav")],
    ("media_unsupported", lambda: sdk.inline_attachment(name="x", type="audio", mime="audio/wav", data=b"bad")),
    ("media_unsupported", lambda: sdk.inline_attachment(name="x", type="audio", mime="audio/wav", data=wav(16000, 6))),
    ("media_unsupported", lambda: sdk.inline_attachment(name="x", type="audio", mime="audio/wav", data=wav(16000, 3, 1, 16))),
    ("invalid_request", lambda: sdk.validate_attachments([IMAGE, IMAGE])),
    ("invalid_request", lambda: sdk.validate_attachments([{**IMAGE, "data": "data:image/png;base64,AAAA="}])),
    ("invalid_request", lambda: sdk.validate_attachments([{**IMAGE, "name": "a" + str(i)} for i in range(17)])),
    ("media_unsupported", lambda: sdk.validate_attachments([{**IMAGE, "name": "a" + str(i)} for i in range(5)])),
    ("media_unsupported", lambda: sdk.validate_attachments([AUDIO, {**AUDIO, "name": "second"}])),
    ("media_too_large", lambda: sdk.validate_attachments([IMAGE, {**IMAGE, "name": "second", "data": url("image/png", PNG + bytes(2621440 - len(PNG)))}])),
])
def test_local_refusals_use_gateway_codes_without_dispatch(code, action):
    seen = []
    with sdk.SystemZero(api_key="SYNTHETIC", transport=httpx.MockTransport(lambda request: seen.append(request))):
        with pytest.raises(sdk.SystemZeroError) as caught:
            action()
    assert caught.value.code == code
    assert seen == []


@pytest.mark.parametrize("kind,code,status", (("document", "document_not_available", 404), ("video", "video_not_available", 404), ("image", "service_unavailable", 503), ("audio", "media_unsupported", 400)))
def test_gateway_refusals_are_preserved(kind, code, status):
    seen = []
    def send(request):
        seen.append(request)
        return httpx.Response(status, json={"error": {"code": code, "message": "Synthetic refusal.", "type": "invalid_request_error", "retryable": False}})
    with sdk.SystemZero(api_key="SYNTHETIC", max_retries=0, transport=httpx.MockTransport(send)) as client:
        with pytest.raises(sdk.SystemZeroError) as caught:
            client.decide({**REQUEST, "attachments": [{"name": "input", "type": kind, "upload": "szu_" + "a" * 32, "mime": "image/png" if kind == "image" else "audio/wav"}]})
    assert caught.value.code == code
    assert len(seen) == 1


def test_function_tools_carry_served_media():
    seen = []
    with sdk.SystemZero(api_key="SYNTHETIC", max_retries=0, transport=httpx.MockTransport(
            lambda request: (seen.append(json.loads(request.content)), httpx.Response(200, json={"answers": {}}))[1])) as client:
        result = create_tool_handler(client)("system_zero_decide", {"state": "Synthetic", "questions": [{"name": "ready", "type": "boolean", "instructions": "Ready?", "criteria": None}], "model": None, "attachments": [IMAGE, AUDIO]})
    assert result["status"] == "ok"
    assert seen[0]["attachments"] == [IMAGE, AUDIO]
