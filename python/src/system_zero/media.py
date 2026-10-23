"""Explicit preflight for served inline media; the API validates complete files."""
import base64
import binascii
import re
import struct
from types import MappingProxyType
from typing import Any, Literal, NoReturn, Optional, Sequence, Union

from .errors import error_class
from .types import Attachment

MAX_INLINE_BYTES = 2621440
MAX_ATTACHMENTS = 16
MIMES = MappingProxyType({'image': ('image/png', 'image/jpeg'), 'audio': ('audio/wav',)})
MEDIA_LIMITS = MappingProxyType({'max_attachments': 16, 'max_images': 4, 'max_audio': 1, 'max_inline_bytes': 2621440, 'max_body_bytes': 3665920, 'audio_sample_rate': 16000, 'max_audio_duration_ms': 300000, 'max_audio_channels': 64, 'max_image_pixels': 16777216, 'max_image_aspect_ratio': 50})
ByteData = Union[bytes, bytearray, memoryview]
_ERRORS = {'invalid_request': [400, 'The System Zero request is invalid.'], 'media_unsupported': [400, 'The media kind or count is not supported. At most 4 images and 1 audio are allowed.'], 'media_too_large': [413, 'The media exceeds the decoded byte limit.'], 'media_decode_failed': [422, 'The media bytes do not match the declared format.'], 'media_unpriced': [403, 'This input type is not supported.']}
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_DATA_HEADER = re.compile(r"data:[a-z0-9.+/-]+;base64\Z")

def _refuse(code: str, param: Optional[str] = None) -> NoReturn:
    status, message = _ERRORS[code]
    raise error_class("invalid_request_error", code)(message, status=status, type="invalid_request_error",
                                                     code=code, param=param, retryable=False)


def _raw_bytes(data: ByteData, cap: Optional[int], code: str, param: str = "data") -> bytes:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        _refuse("invalid_request", param)
    size = data.nbytes if isinstance(data, memoryview) else len(data)
    if cap is not None and size > cap:
        _refuse(code, param)
    return bytes(data)


def _signature_matches(mime: str, data: bytes) -> bool:
    if mime == 'image/png':
        return data.startswith(b'\x89PNG\r\n\x1a\n')
    if mime == 'image/jpeg':
        return data.startswith(b'\xff\xd8\xff')
    if mime == 'image/gif':
        return data.startswith((b'GIF87a', b'GIF89a'))
    if mime in {'image/webp', 'audio/wav'}:
        return data[:4] == b'RIFF' and data[8:12] == (b'WEBP' if mime == 'image/webp' else b'WAVE')
    return False


def attachment_magic_matches(mime: str, data: ByteData) -> bool:
    """Check a served signature without decoding the complete image."""
    return any(mime in values for values in MIMES.values()) and _signature_matches(mime, bytes(data))


def _need(value: Any) -> None:
    if not value:
        _refuse("media_unsupported", "attachments")


def _audio_frames(raw: bytes) -> int:
    try:
        _need(len(raw) >= 12 and raw[:4] == b"RIFF" and struct.unpack_from("<I", raw, 4)[0] == len(raw) - 8 and raw[8:12] == b"WAVE")
        pos, align, rate, size_data, found_format = 12, 0, 0, -1, False
        while pos < len(raw):
            _need(pos + 8 <= len(raw))
            kind, size = raw[pos:pos + 4], struct.unpack_from("<I", raw, pos + 4)[0]
            start = pos + 8
            _need(start + size + size % 2 <= len(raw))
            if kind == b"fmt ":
                _need(not found_format and size >= 16)
                found_format = True
                codec, channels, rate, byte_rate, align, bits = struct.unpack_from("<HHIIHH", raw, start)
                _need(0 < channels <= MEDIA_LIMITS["max_audio_channels"] and 0 < rate <= 768000 and 0 < bits <= 64)
                if codec == 0xfffe:
                    _need(size >= 40)
                    extra, valid = struct.unpack_from("<HH", raw, start + 16)
                    _need(extra >= 22 and 18 + extra <= size and 0 < valid <= bits)
                    _need(raw[start + 28:start + 40] == bytes.fromhex("00001000800000aa00389b71"))
                    codec = struct.unpack_from("<I", raw, start + 24)[0]
                elif size > 16:
                    _need(size >= 18 and 18 + struct.unpack_from("<H", raw, start + 16)[0] <= size)
                _need(codec == 1 or codec == 3 and bits in (32, 64))
                _need(align == channels * ((bits + 7) // 8) and byte_rate == rate * align)
            elif kind == b"data":
                _need(size_data == -1)
                size_data = size
            pos = start + size + size % 2
        _need(found_format and size_data > 0 and size_data % align == 0)
        _need(rate == MEDIA_LIMITS["audio_sample_rate"])
        return size_data // align
    except (struct.error, ZeroDivisionError):
        _refuse("media_unsupported", "attachments")


def _identity(name: Any, kind: Any) -> None:
    if not isinstance(name, str) or _NAME.fullmatch(name) is None or not isinstance(kind, str) or kind not in MIMES:
        _refuse("invalid_request", "attachments")


def _content(kind: str, mime: str, raw: bytes, param: str) -> None:
    if kind == "audio":
        if mime != "audio/wav":
            _refuse("media_unsupported", param)
        _audio_frames(raw)
        return
    if mime in ("image/gif", "image/webp"):
        if not _signature_matches(mime, raw):
            _refuse("media_decode_failed", param)
        _refuse("media_unpriced", param)
    if mime not in MIMES[kind]:
        _refuse("media_unsupported", param)
    if not _signature_matches(mime, raw):
        _refuse("media_decode_failed", param)


def inline_attachment(*, name: str, type: Literal['image', 'audio'], mime: str, data: ByteData) -> Attachment:
    """Encode one served data URL with bounded, caller-known checks."""
    _identity(name, type)
    raw = _raw_bytes(data, MAX_INLINE_BYTES, "media_too_large")
    _content(type, mime, raw, "attachments")
    return {"name": name, "type": type, "data": "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")}


def validate_attachments(items: Sequence[Attachment]) -> Sequence[Attachment]:
    """Check inline inputs; availability, references and full decoding remain with the API."""
    if not isinstance(items, (list, tuple)):
        _refuse("invalid_request", "attachments")
    if any(isinstance(item, dict) and ("upload" in item or item.get("type") in ("document", "video")) for item in items):
        return items
    if len(items) > MAX_ATTACHMENTS:
        _refuse("invalid_request", "attachments")
    names = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"name", "type", "data"}:
            _refuse("invalid_request", "attachments")
        _identity(item["name"], item["type"])
        if not isinstance(item["data"], str) or not item["data"].startswith("data:") or item["name"] in names:
            _refuse("invalid_request", "attachments")
        names.add(item["name"])
    counts, total = {"image": 0, "audio": 0}, 0
    for index, item in enumerate(items):
        kind = item["type"]
        counts[kind] += 1
        if counts["image"] > MEDIA_LIMITS["max_images"] or counts["audio"] > MEDIA_LIMITS["max_audio"]:
            _refuse("media_unsupported", "attachments")
        param = "attachments[%d].data" % index
        header, comma, encoded = item["data"].partition(",")
        if not comma or _DATA_HEADER.fullmatch(header) is None:
            _refuse("invalid_request", param)
        size = len(encoded) / 4 * 3 - (2 if encoded.endswith("==") else 1 if encoded.endswith("=") else 0)
        if size > MAX_INLINE_BYTES - total:
            _refuse("media_too_large", param)
        try:
            raw = base64.b64decode(encoded, validate=True)
            if base64.b64encode(raw).decode("ascii") != encoded:
                _refuse("invalid_request", param)
        except (ValueError, binascii.Error):
            _refuse("invalid_request", param)
        _content(kind, header[5:-7], raw, param)
        total += len(raw)
    return items
