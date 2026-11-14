"""Internal package-selected protocol names; never a public client option."""
from dataclasses import dataclass as _dataclass, replace as _replace
from typing import Literal as _Literal
import sys as _sys

_VERSION = "0.5.1"
_DEFAULT_BASE_URL = "https://api.rightnowai.co/v1"
_DANGER_ZONE_HEADERS = ("SZ-Danger-Zone", "RightNow-Danger-Zone")
_StreamProtocol = _Literal["system-zero.stream.v1", "rightnow-decision.stream.v1"]
_DecisionTraceObject = _Literal["system_zero.decision_trace", "decision_model.decision_trace"]


def _resource_path(resource):
    return "/system-zero/" + resource


def _decision_model_path(resource):
    return "/decision-model/" + resource


@_dataclass(frozen=True)
class _Codes:
    timeout: str = "system_zero_timeout"
    unavailable: str = "system_zero_unavailable"
    serving_unattested: str = "system_zero_serving_unattested"


@_dataclass(frozen=True)
class _Wire:
    name: str = "system-zero"
    model: str = "system-zero"
    versioned_model: str = "system-zero-3.0.0"
    decide: str = _resource_path("decide")
    decisions: str = _resource_path("decisions")
    trace: str = "/trace"
    sessions: str = _resource_path("sessions")
    uploads: str = _resource_path("uploads")
    models: str = "/models"
    openapi: str = "/openapi.json"
    danger_zone_header: str = _DANGER_ZONE_HEADERS[0]
    danger_zone_response_header: str = "sz-danger-zone"
    codes: _Codes = _Codes()
    user_agent: str = "system-zero-python/" + _VERSION + " python/" + ".".join(map(str, _sys.version_info[:3]))
    # Compatibility packages retain the legacy stream and webhook names.
    stream_url: str = "wss://api.rightnowai.co/v1/system-zero/stream"
    stream_protocol: _StreamProtocol = "system-zero.stream.v1"
    webhooks: str = _resource_path("webhooks")
    capabilities_header: str = "X-SZ-Capabilities"
    idempotency_prefix: str = "sz-sdk-"


_LEGACY = _Wire()
# The RightNow decision model's names, live on the gateway since names PR 1 to 3 (2026-11-13): the route family
# /v1/decision-model/*, the ids rightnow-decision and rightnow-decision-X.Y.Z, the RightNow-Danger-Zone header and its
# rightnow-danger-zone answer, and the model_* codes. Stream and webhook names are live since PRs 5 and 6.
_RIGHTNOW = _replace(
    _LEGACY,
    name="rightnow-decision",
    stream_url="wss://api.rightnowai.co/v1/decision-model/stream",
    stream_protocol="rightnow-decision.stream.v1",
    webhooks=_decision_model_path("webhooks"),
    model="rightnow-decision",
    versioned_model="rightnow-decision-3.0.0",
    decide=_decision_model_path("decide"),
    decisions=_decision_model_path("decisions"),
    sessions=_decision_model_path("sessions"),
    uploads=_decision_model_path("uploads"),
    models=_decision_model_path("models"),
    openapi=_decision_model_path("openapi.json"),
    danger_zone_header=_DANGER_ZONE_HEADERS[1],
    danger_zone_response_header = "rightnow-danger-zone",
    codes=_Codes(timeout="model_timeout", unavailable="model_unavailable", serving_unattested="model_serving_unattested"),
    user_agent="rightnow-python/" + _VERSION + " python/" + ".".join(map(str, _sys.version_info[:3])),
)


def _wire_for(name="rightnow-decision"):
    return {"system-zero": _LEGACY, "rightnow-decision": _RIGHTNOW}[name]


def _client_wire(client=None):
    return getattr(client, "_wire", _RIGHTNOW)
