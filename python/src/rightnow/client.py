"""Synchronous server-side RightNow decision model HTTP API client."""

from ._wire import _wire_for, _DANGER_ZONE_HEADERS, _DEFAULT_BASE_URL, _VERSION
from ._redaction import redact, Secret, protected, reveal

from dataclasses import replace as _replace
from email.utils import parsedate_to_datetime as _parsedate
import ipaddress
import json
import math
import os
import random as random_module
import re
import sys
import time
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping, Optional, Union, cast
from uuid import uuid4
from urllib.parse import urlsplit

import httpx

from ._transport import _request
from ._transport import AttemptState, default_transport, attempt_state
from ._connection import ResponseCloseGuard, connection_error, is_transport_error
from ._retry import _IDEMPOTENCY_UNSET, _idempotency_mode, _recover_workflow, _retry_context, _retry_response, _retry_wait
from .decision_records import DecisionRecords, DecisionTrace
from .errors import APIConnectionError, APIError, APITimeoutError, InvalidRequestError, KeyNotSupportedError, LongContextNotAvailableError, SystemZeroError, error_class
from .policy import validate_request_policy
from .response import ResponseResult, response_metadata
from .types import DecideReply, DecideRequest, ModelList
from .webhooks import Webhooks
from .enabled_types import EnabledDecideRequest, VideoRequest


VERSION = _VERSION
DEFAULT_BASE_URL = _DEFAULT_BASE_URL
_MAX_TIMEOUT_SECONDS = 2_147_483_647 / 1000


def _checked_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        local = host == "".join(("local", "host"))
        if host and not local:
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        if (parsed.scheme != "https" and not (parsed.scheme == "http" and local)
                or not host or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or "?" in value or "#" in value):
            raise ValueError()
        parsed.port
    except (AttributeError, TypeError, ValueError):
        raise ValueError("base_url must be HTTPS or loopback HTTP without credentials, query or fragment.") from None
    return value.rstrip("/")


def _validate_retry_budget(value: float) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not 0 <= value <= _MAX_TIMEOUT_SECONDS):
        raise ValueError("retry_budget must be nonnegative, finite and at most 2147483.647 seconds.")
    return value


def _api_error(response: httpx.Response, body: Any) -> SystemZeroError:
    headers = dict(response.headers)
    envelope = body if isinstance(body, dict) else {}
    error = envelope.get("error")
    if isinstance(error, dict):
        kind = error.get("type") if isinstance(error.get("type"), str) else "api_error"
        message = error.get("message") if isinstance(error.get("message"), str) else "RightNow decision model API request failed."
        code = error.get("code") if isinstance(error.get("code"), str) else None
        cls = error_class(kind, code)
        param = error.get("param") if isinstance(error.get("param"), str) else None
        retryable = error.get("retryable") if isinstance(error.get("retryable"), bool) else _retry_response(response.status_code, code, headers)
        request_id = envelope.get("request_id") if isinstance(envelope.get("request_id"), str) else headers.get("x-request-id")
        details = error.get("details") if isinstance(error.get("details"), dict) else None
        return cls(message, status=response.status_code, type=kind, code=code, param=param,
                   retryable=retryable, details=details, request_id=request_id, headers=headers,
                   limit_cents=error.get("limit_cents"), spent_cents=error.get("spent_cents"), resets_at=error.get("resets_at"))
    return APIError(
        "RightNow decision model API returned a non-JSON or invalid response.", status=response.status_code,
        retryable=_retry_response(response.status_code, None, headers),
        request_id=headers.get("x-request-id"), headers=headers,
    )


class Models:
    """The authenticated GET /decision-model/models resource."""

    def __init__(self, client: "RightNow"):
        self._client = client

    def list(self, *, client_request_id: Optional[str] = None, headers: Optional[Mapping[str, str]] = None,
             retry_budget: Optional[float] = None) -> ModelList:
        return self.list_with_response(client_request_id=client_request_id, headers=headers, retry_budget=retry_budget).data

    def list_with_response(self, *, client_request_id: Optional[str] = None,
                           headers: Optional[Mapping[str, str]] = None,
                           retry_budget: Optional[float] = None) -> ResponseResult[ModelList]:
        return cast(ResponseResult[ModelList], self._client._request(
            "GET", self._client._wire.models, client_request_id=client_request_id, headers=headers, retry_budget=retry_budget,
        ))


def _long_context_unavailable(param: str) -> None:
    raise LongContextNotAvailableError("This request shape is not available for the selected long context class.",
        status=404, type="not_found_error", code="long_context_not_available", param=param)


# A key that starts with rp_ is served by POST /decisions alone, and only for decide. That route takes the decision in its
# own request shape and answers in its own shape; the client writes the one and reads the other into the decide types, so a
# caller's code is the same for both kinds of key. Everything it does not serve is refused here, before any network call.
_RP_PREFIX = "rp_"
_RP_DECIDE_PATH = "/decisions"
# The most questions that route takes in one call.
_RP_MAX_QUESTIONS = 32
# The request members that route serves. prompt_cache_key is sent so one program works with both kinds of key; it is not
# used there yet.
_RP_MEMBERS = frozenset({"model", "state", "questions", "attachments", "route", "mode", "prompt_cache_key"})
_MILLION = 1_000_000
_RP_METHOD_MESSAGE = ("A key that starts with rp_ calls decide() only. Streaming, uploads, webhooks, decision lookups and "
                      "the model list are not available with it yet.")


def _rp_refusal(message: str, param: Optional[str]) -> KeyNotSupportedError:
    return KeyNotSupportedError(message, status=400, type="invalid_request_error", code="key_not_supported",
                                param=param, retryable=False)


def _rp_member(name: str) -> KeyNotSupportedError:
    return _rp_refusal(name + " is not available with a key that starts with rp_ yet. Send decide() without it.", name)


def _rp_body(request: Mapping[str, Any], danger_zone: Optional[str]) -> dict:
    """The decide request in that route's shape: a boolean question is its noul question, and the model ids are its ids."""
    # That route never receives the class header: the call would be answered without it.
    if danger_zone is not None:
        raise _rp_member("danger_zone")
    # It refuses session_id and drops these members unread, so the call would be answered and charged without them.
    for name in ("session_id", "policy"):
        if name in request:
            raise _rp_member(name)
    # Any other member would be answered and charged without it, so it is refused too.
    for name in request:
        if name not in _RP_MEMBERS:
            raise _rp_member(str(name))
    attachments = request.get("attachments")
    if attachments is not None and not (isinstance(attachments, list) and not attachments):
        raise _rp_member("attachments")
    for name in ("route", "mode"):
        if name in request and request[name] != "standard":
            raise _rp_member(name)
    questions = request.get("questions")
    if isinstance(questions, Mapping) and len(questions) > _RP_MAX_QUESTIONS:
        raise _rp_refusal("A key that starts with rp_ takes at most 32 questions in one decide() call. Send the rest in "
                          "another call.", "questions")
    body = dict(request)
    # That route refuses the attachments member whenever it is present; None or empty means none, so it is not sent.
    body.pop("attachments", None)
    model = request.get("model")
    if isinstance(model, str):
        body["model"] = re.sub(r"\Arightnow-decision(?=\Z|-[0-9]+\.[0-9]+\.[0-9]+\Z)", "system-zero", model)
    if isinstance(questions, Mapping):
        body["questions"] = {name: {**question, "type": "noul"}
                             if isinstance(question, Mapping) and question.get("type") == "boolean" else question
                             for name, question in questions.items()}
    return body


def _rp_units(value: Any) -> Optional[int]:
    if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1:
        return math.floor(value * _MILLION + 0.5)
    return None


def _rp_answer(answer: Any) -> Any:
    """One answer of that route read as the decide answer.

    It names no calibration, so calibration_version is "unpublished" and calibrated is False. A refused question names no
    code, so the refusal's code is decision_refused. Any other shape is kept.
    """
    if not isinstance(answer, Mapping):
        return answer
    kind = answer.get("type")
    if kind == "refusal":
        return {"error": {"code": "decision_refused", "retryable": False}}
    if kind == "noul":
        units = _rp_units(answer.get("noul"))
        if units is None:
            return answer
        yes = units >= _MILLION / 2
        return {"type": "boolean", "probability": units / _MILLION,
                "confidence": max(units, _MILLION - units) / _MILLION, "calibration_version": "unpublished",
                "decision": yes, "alternatives": [{"decision": not yes, "confidence": min(units, _MILLION - units) / _MILLION}],
                "calibrated": False}
    probabilities = answer.get("probabilities")
    if kind in ("choice", "score") and isinstance(probabilities, Mapping):
        units = {}
        for key, value in probabilities.items():
            amount = _rp_units(value)
            if amount is None or not isinstance(key, str):
                return answer
            units[key] = amount
        score = kind == "score"
        if score and any(not key.isascii() or not key.isdecimal() for key in units):
            return answer
        # Highest first; a tie goes to the lower level of a score and to the lower key bytes otherwise.
        order = sorted(units, key=lambda key: (-units[key], float(key) if score else key.encode("utf-8")))
        selected = order[0] if score else answer.get("choice")
        if not isinstance(selected, str) or selected not in units:
            return answer
        fields = {"confidence": units[selected] / _MILLION, "calibration_version": "unpublished", "decision": selected,
                  "alternatives": [{"decision": key, "confidence": units[key] / _MILLION} for key in order if key != selected],
                  "calibrated": False}
        values = {key: amount / _MILLION for key, amount in units.items()}
        if score:
            return {"type": "score", "score": answer.get("score"), "legend": answer.get("legend"), "probabilities": values, **fields}
        return {"type": "choice", "choice": selected, "probabilities": values, **fields}
    return answer


def _rp_count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _rp_reply(body: Any, model: Any, headers: Mapping[str, str], now: Callable[[], float]) -> dict:
    """That route's answer read as the decide reply.

    It sends no long input, no media items and no thought tokens: those counts are 0. context.tokens is its input token
    count, and created is its Date header, or this client's clock without one. usage.cost (USD) and usage.cost_nanocents
    are the call's charge as it states it.
    """
    if not isinstance(body, Mapping) or not isinstance(body.get("answers"), Mapping) or not isinstance(body.get("usage"), Mapping):
        raise APIError("RightNow decision model API returned a non-JSON or invalid response.", status=200,
                       request_id=headers.get("x-request-id"), headers=headers)
    usage = body["usage"]
    details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), Mapping) else {}
    served = body.get("model")
    # It names the served version by the old ids; a caller who sent the decision model's ids reads those ids back.
    if isinstance(served, str) and isinstance(model, str) and model.startswith("rightnow-decision"):
        served = re.sub(r"\Asystem-zero(?=\Z|-)", "rightnow-decision", served)
    try:
        created = math.floor(_parsedate(headers.get("date", "")).timestamp())
    except (TypeError, ValueError, IndexError, OverflowError):
        created = math.floor(now())
    reply = {
        "id": body.get("id"), "model": served, "served_by": served, "created": created,
        "context": {"route": "direct", "tokens": _rp_count(usage.get("input_tokens"))},
        "answers": {name: _rp_answer(answer) for name, answer in body["answers"].items()},
        "usage": {"input_tokens": _rp_count(usage.get("input_tokens")), "cached_input_tokens": _rp_count(details.get("cached_tokens")),
                  "media_tokens": _rp_count(usage.get("media_tokens")), "long_input_tokens": 0, "media_items": 0, "thought_tokens": 0},
    }
    if type(usage.get("cost")) in (int, float):
        reply["usage"]["cost"] = usage["cost"]
    if isinstance(usage.get("cost_nanocents"), str):
        reply["usage"]["cost_nanocents"] = usage["cost_nanocents"]
    return reply


def _rp_decide(client: Any, request: Mapping[str, Any], *, idempotency_key: Optional[str], client_request_id: Optional[str],
               idempotency: Any, headers: Optional[Mapping[str, str]], retry_budget: Optional[float],
               danger_zone: Optional[str]) -> ResponseResult[DecideReply]:
    """decide with a key that starts with rp_: POST /decisions in that route's request shape, read back as the decide reply."""
    body = _rp_body(request, danger_zone)
    result = client._request("POST", "/decisions", body=body, idempotency_key=idempotency_key,
                             client_request_id=client_request_id, headers=headers, retry_budget=retry_budget,
                             idempotency=idempotency)
    # That route marks a stored answer it sends again with its own header of the same form.
    replayed = result.metadata.replayed or any(
        re.fullmatch(r"x-[a-z]+-idempotent-replay", name.lower()) is not None and value == "true" for name, value in result.headers.items())
    metadata = _replace(result.metadata, replayed=True) if replayed and not result.metadata.replayed else result.metadata
    return ResponseResult(cast(DecideReply, _rp_reply(result.data, request.get("model"), result.headers, client._now)),
                          metadata, result.status, result.headers)


class RightNow:
    """Server-side client. Do not ship API keys to a browser.

    Timeouts and retry waits are seconds. The client owns its HTTPX client and
    injected transport; close it or use a context manager. Default clients share
    a process-wide connection pool that remains open until process shutdown.
    The sleep/random/now hooks let tests control pacing without real delays.
    """

    _wire = _wire_for()

    def __init__(
        self, api_key: Optional[str] = None, *, base_url: str = DEFAULT_BASE_URL,
        timeout: float = 150.0, max_retries: int = 5, max_retry_wait: float = 60.0,
        retry_budget: float = 60.0, compress_requests: bool = True,
        idempotency: Literal["off", "auto"] = "off",
        transport: Optional[httpx.BaseTransport] = None,
        sleep: Callable[[float], None] = time.sleep,
        random: Callable[[], float] = random_module.random,
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        key = api_key if api_key is not None else os.environ.get("RIGHTNOW_API_KEY", os.environ.get("SYSTEM_ZERO_API_KEY"))
        if key is None:
            raise ValueError("Set api_key or RIGHTNOW_API_KEY.")
        if not isinstance(key, str) or not key or any(not 0x21 <= ord(character) <= 0x7e for character in key):
            raise ValueError("api_key must contain only visible ASCII characters.")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be a nonnegative integer.")
        if not isinstance(compress_requests, bool):
            raise ValueError("compress_requests must be a boolean.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= _MAX_TIMEOUT_SECONDS:
            raise ValueError("timeout must be positive, finite and at most 2147483.647 seconds.")
        if (isinstance(max_retry_wait, bool) or not isinstance(max_retry_wait, (int, float)) or
                not 0 <= max_retry_wait <= _MAX_TIMEOUT_SECONDS):
            raise ValueError("max_retry_wait must be nonnegative, finite and at most 2147483.647 seconds.")
        checked_base_url = _checked_base_url(base_url)
        self._api_key = Secret(key)
        # The key starts with rp_: decide goes to POST /decisions and every other method is refused.
        self._rp_only = key.startswith(_RP_PREFIX)
        self.base_url = checked_base_url
        self.timeout = timeout
        self.max_retries = min(max_retries, 5)
        self.max_retry_wait = max_retry_wait
        self.retry_budget = _validate_retry_budget(retry_budget)
        self.idempotency = _idempotency_mode(idempotency)
        self._compress_requests = compress_requests
        self._sleep = sleep
        self._random = random
        self._now = now
        self._monotonic = monotonic
        self._closed = False
        self._http = httpx.Client(
            transport=transport if transport is not None else default_transport(),
            timeout=timeout, follow_redirects=False, trust_env=False,
        )
        self.models = Models(self)
        self.webhooks = Webhooks(self)

    def __enter__(self) -> "RightNow":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    @protected
    def close(self) -> None:
        if not self._closed:
            self._closed = True
            context_boundary = sys.exc_info()[1]
            try:
                self._http.close()
            except Exception as cause:
                if not is_transport_error(cause, context_boundary=context_boundary):
                    raise
                raise connection_error(cause, "RightNow connection cleanup failed.",
                                       context_boundary=context_boundary) from None

    def get_decision(self, id: str, *, client_request_id: Optional[str] = None,
                     headers: Optional[Mapping[str, str]] = None,
                     retry_budget: Optional[float] = None) -> DecideReply:
        """Read a retained decision made with this API key."""
        return DecisionRecords(self).get_decision(id, client_request_id=client_request_id,
                                                 headers=headers, retry_budget=retry_budget)

    def get_decision_with_response(self, id: str, *, client_request_id: Optional[str] = None,
                                   headers: Optional[Mapping[str, str]] = None,
                                   retry_budget: Optional[float] = None) -> ResponseResult[DecideReply]:
        """Read a retained decision with HTTP response metadata."""
        return DecisionRecords(self).get_decision_with_response(id, client_request_id=client_request_id,
                                                               headers=headers, retry_budget=retry_budget)

    def get_decision_trace(self, id: str, *, client_request_id: Optional[str] = None,
                           headers: Optional[Mapping[str, str]] = None,
                           retry_budget: Optional[float] = None) -> DecisionTrace:
        """Read the content-free trace for a retained decision."""
        return DecisionRecords(self).get_decision_trace(id, client_request_id=client_request_id,
                                                       headers=headers, retry_budget=retry_budget)

    def get_decision_trace_with_response(self, id: str, *, client_request_id: Optional[str] = None,
                                         headers: Optional[Mapping[str, str]] = None,
                                         retry_budget: Optional[float] = None) -> ResponseResult[DecisionTrace]:
        """Read a retained trace with HTTP response metadata."""
        return DecisionRecords(self).get_decision_trace_with_response(id, client_request_id=client_request_id,
                                                                     headers=headers, retry_budget=retry_budget)

    getDecision = get_decision
    getDecisionTrace = get_decision_trace

    def decide(
        self, request: Union[DecideRequest, EnabledDecideRequest, VideoRequest, Mapping[str, Any]], *,
        idempotency_key: Optional[str] = None, client_request_id: Optional[str] = None,
        idempotency: Literal["off", "auto"] = _IDEMPOTENCY_UNSET,
        headers: Optional[Mapping[str, str]] = None,
        retry_budget: Optional[float] = None,
        danger_zone: Optional[Literal["accept", "long_context_64k", "long_context_128k", "long_context_1m"]] = None,
    ) -> DecideReply:
        return self.decide_with_response(request, idempotency_key=idempotency_key, idempotency=idempotency,
                                         client_request_id=client_request_id, headers=headers,
                                         retry_budget=retry_budget, danger_zone=danger_zone).data

    def decide_with_response(
        self, request: Union[DecideRequest, EnabledDecideRequest, VideoRequest, Mapping[str, Any]], *,
        idempotency_key: Optional[str] = None, client_request_id: Optional[str] = None,
        idempotency: Literal["off", "auto"] = _IDEMPOTENCY_UNSET,
        headers: Optional[Mapping[str, str]] = None,
        retry_budget: Optional[float] = None,
        danger_zone: Optional[Literal["accept", "long_context_64k", "long_context_128k", "long_context_1m"]] = None,
    ) -> ResponseResult[DecideReply]:
        if self._rp_only:
            return _rp_decide(self, request, idempotency_key=idempotency_key, client_request_id=client_request_id,
                              idempotency=idempotency, headers=headers, retry_budget=retry_budget, danger_zone=danger_zone)
        if danger_zone == "long_context_1m":
            if not isinstance(request.get("state"), str):
                _long_context_unavailable("state")
            if not isinstance(request.get("questions"), Mapping) or len(request["questions"]) != 1:
                raise InvalidRequestError("The 1M class requires exactly one question.", status=413,
                    type="invalid_request_error", code="questions_too_large", param="questions")
            for name in ("policy", "mode", "route", "session_id", "prompt_cache_key"):
                if name in request:
                    _long_context_unavailable(name)
        validate_request_policy(request)
        outgoing = httpx.Headers(headers)
        if any(name in outgoing for name in _DANGER_ZONE_HEADERS) or (danger_zone is not None and danger_zone not in ("accept", "long_context_64k", "long_context_128k", "long_context_1m")):
            raise InvalidRequestError("Use danger_zone with accept, long_context_64k, long_context_128k or long_context_1m; do not supply its header.",
                                      status=400, type="invalid_request_error", code="invalid_request", param=self._wire.danger_zone_header)
        return cast(ResponseResult[DecideReply], self._request(
            "POST", self._wire.decide, body=request,
            idempotency_key=idempotency_key, client_request_id=client_request_id,
            headers=outgoing, retry_budget=retry_budget, _danger_zone=danger_zone, idempotency=idempotency,
        ))

    _request = _request

# Deprecated: use RightNow. Kept as an identical alias for existing imports.
SystemZero = RightNow
