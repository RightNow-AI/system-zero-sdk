"""Provider-independent function tools with a local call limit and stable retry keys."""

from copy import deepcopy
from decimal import Decimal
import hashlib
from importlib.resources import files as _resource_files
import json
import math
import os
import re
from pathlib import Path
import struct
from threading import Lock
from typing import Any, Dict, Optional

from system_zero import InvalidRequestError, SystemZero, SystemZeroError
from system_zero.client import _checked_base_url
from system_zero._retry import _retry_context


_HERE = _resource_files(__package__) if __package__ else Path(__file__).resolve().parent
_DEFINITIONS = json.loads(_HERE.joinpath("tool-schemas.json").read_text(encoding="utf-8"))
_ERRORS = json.loads(_HERE.joinpath("error-catalog.json").read_text(encoding="utf-8"))
_ARGUMENT_LIMITS = json.loads(_HERE.joinpath("argument-limits.json").read_text(encoding="utf-8"))
_SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in _DEFINITIONS}
_MAX_SAFE_INTEGER = 9_007_199_254_740_991

OPENAI_TOOLS = [
    {"type": "function", "function": {
        "name": tool["name"], "description": tool["description"],
        "parameters": deepcopy(tool["inputSchema"]), "strict": True,
    }}
    for tool in _DEFINITIONS
]
OPENAI_RESPONSES_TOOLS = [
    {"type": "function", **deepcopy(tool["function"])} for tool in OPENAI_TOOLS
]
ANTHROPIC_TOOLS = [
    {"name": tool["name"], "description": tool["description"], "input_schema": deepcopy(tool["inputSchema"])}
    for tool in _DEFINITIONS
]


def _invalid() -> InvalidRequestError:
    return InvalidRequestError(
        _ERRORS["invalid_request"]["message"], code="invalid_request",
        status=_ERRORS["invalid_request"]["status"], type="invalid_request_error",
    )


def _reject_constant(_value: str) -> None:
    raise _invalid()


def _json_number(value: str) -> Any:
    # The TypeScript provider boundary uses JSON.parse's IEEE754 numeric domain.
    number = float(value)
    if not math.isfinite(number):
        raise _invalid()
    if number.is_integer() and abs(number) < 1e21:
        # Expand the shortest decimal, not the binary float's exact integer.
        return int(Decimal(str(number)))
    return number


def _screen_json(value: str, values: int = 0) -> int:
    """Bound raw input before JSON parsing, including duplicate object names."""
    if len(value) > _ARGUMENT_LIMITS["maxBytes"]:
        raise _invalid()
    byte_count = 0
    for character in value:
        unit = ord(character)
        if 0xD800 <= unit <= 0xDFFF:
            raise _invalid()
        byte_count += 1 if unit < 128 else 2 if unit < 2048 else 3 if unit < 65536 else 4
        if byte_count > _ARGUMENT_LIMITS["maxBytes"]:
            raise _invalid()
    depth = 0
    index = 0
    while index < len(value):
        character = value[index]
        index += 1
        if character in " \t\r\n:,":
            continue
        if character in "}]":
            depth -= 1
            if depth < 0:
                raise _invalid()
            continue
        values += 1
        if values > _ARGUMENT_LIMITS["maxValues"]:
            raise _invalid()
        if character in "{[":
            depth += 1
            if depth > _ARGUMENT_LIMITS["maxDepth"]:
                raise _invalid()
        elif character == '"':
            closed = False
            while index < len(value):
                character = value[index]
                index += 1
                if character == "\\":
                    index += 1
                elif character == '"':
                    closed = True
                    break
            if not closed:
                raise _invalid()
        else:
            # Syntax and finite numbers are still checked by the JSON parser.
            while index < len(value) and value[index] not in "{}[],: \t\r\n":
                index += 1
    if depth:
        raise _invalid()
    return values


def _screen_arguments(value: Any, raw_values: Optional[int] = None) -> None:
    """Bound objects and encoded documents before making the owned snapshot."""
    byte_count = 0
    values = 0 if raw_values is None else raw_values
    active = set()

    def add_bytes(amount: int) -> None:
        nonlocal byte_count
        byte_count += amount
        if byte_count > _ARGUMENT_LIMITS["maxBytes"]:
            raise _invalid()

    def add_value() -> None:
        nonlocal values
        # Raw input already counted every outer token, including duplicate names.
        if raw_values is None:
            values += 1
            if values > _ARGUMENT_LIMITS["maxValues"]:
                raise _invalid()

    def string(text: str) -> None:
        if len(text) > _ARGUMENT_LIMITS["maxBytes"] - byte_count:
            raise _invalid()
        add_bytes(2)
        for character in text:
            unit = ord(character)
            if 0xD800 <= unit <= 0xDFFF:
                raise _invalid()
            if unit in (34, 92, 8, 9, 10, 12, 13):
                add_bytes(2)
            elif unit < 32:
                add_bytes(6)
            else:
                add_bytes(1 if unit < 128 else 2 if unit < 2048 else 3 if unit < 65536 else 4)

    def visit(item: Any, depth: int) -> None:
        nonlocal values
        add_value()
        if item is None:
            add_bytes(4)
            return
        if type(item) is bool:
            add_bytes(4 if item else 5)
            return
        if type(item) is str:
            string(item)
            return
        if type(item) in (int, float):
            if not math.isfinite(item):
                raise _invalid()
            # Finiteness bounds int conversion to 309 digits plus a possible sign.
            # Floats share TypeScript's conservative maximum spelling allowance.
            add_bytes(max(25, len(str(item))) if type(item) is int else 25)
            return
        if type(item) not in (dict, list) or id(item) in active:
            raise _invalid()
        depth += 1
        if depth > _ARGUMENT_LIMITS["maxDepth"]:
            raise _invalid()
        active.add(id(item))
        add_bytes(2)
        if type(item) is list:
            if raw_values is None and len(item) > _ARGUMENT_LIMITS["maxValues"] - values:
                raise _invalid()
            for index, child in enumerate(item):
                if index:
                    add_bytes(1)
                visit(child, depth)
        else:
            for index, (key, child) in enumerate(item.items()):
                if type(key) is not str:
                    raise _invalid()
                if index:
                    add_bytes(1)
                add_value()
                string(key)
                add_bytes(1)
                visit(child, depth)
                if key == "json" and type(child) is str:
                    values = _screen_json(child, values)
        active.remove(id(item))

    visit(value, 0)


def _plain_json(value: Any) -> None:
    """Reject non-JSON values without adding gateway size or media policies."""
    active = set()
    stack = [(value, False)]
    while stack:
        item, leaving = stack.pop()
        if leaving:
            active.remove(id(item))
            continue
        if item is None or type(item) is bool:
            continue
        if type(item) is str:
            item.encode("utf-8")
            continue
        if type(item) in (int, float):
            if not math.isfinite(item):
                raise _invalid()
            continue
        if type(item) not in (dict, list) or id(item) in active:
            raise _invalid()
        active.add(id(item))
        stack.append((item, True))
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise _invalid()
                key.encode("utf-8")
                stack.append((child, False))
        else:
            stack.extend((child, False) for child in item)


def _parse_json(value: str, *, screened: bool = False) -> Any:
    if not screened:
        _screen_json(value)
    result = json.loads(value, parse_int=_json_number, parse_float=_json_number, parse_constant=_reject_constant)
    _plain_json(result)
    return result


def _matches(schema: Dict[str, Any], value: Any) -> bool:
    if "anyOf" in schema:
        return any(_matches(branch, value) for branch in schema["anyOf"])
    expected = schema.get("type")
    if isinstance(expected, list):
        return any(_matches({**schema, "type": item}, value) for item in expected)
    valid = {
        "object": type(value) is dict,
        "array": type(value) is list,
        "string": type(value) is str,
        "null": value is None,
        "boolean": type(value) is bool,
        "number": type(value) in (int, float),
        "integer": type(value) is int,
    }.get(expected, False)
    if not valid or ("enum" in schema and value not in schema["enum"]):
        return False
    if expected == "object":
        properties = schema.get("properties", {})
        if not set(schema.get("required", [])).issubset(value):
            return False
        if schema.get("additionalProperties") is False and not set(value).issubset(properties):
            return False
        return all(_matches(properties[key], child) for key, child in value.items() if key in properties)
    if expected == "array":
        return all(_matches(schema["items"], child) for child in value)
    return True


def _structured(value: Any) -> Any:
    if type(value) is str:
        return value
    result = _parse_json(value["json"])
    if type(result) not in (dict, list):
        raise _invalid()
    return result


def _arguments(name: str, value: Any) -> Dict[str, Any]:
    if type(name) is not str or name not in _SCHEMAS:
        raise _invalid()
    raw_values = _screen_json(value) if type(value) is str else None
    parsed = _parse_json(value, screened=True) if raw_values is not None else value
    _screen_arguments(parsed, raw_values)
    if type(parsed) is not dict:
        raise _invalid()
    parsed = deepcopy(parsed)
    if name == "system_zero_decide":
        parsed.setdefault("model", None)
        parsed.setdefault("attachments", None)
        if type(parsed.get("questions")) is list:
            for question in parsed["questions"]:
                if type(question) is dict and question.get("type") == "boolean":
                    question.setdefault("criteria", None)
    if not _matches(_SCHEMAS[name], parsed):
        raise _invalid()
    return parsed


def _request(arguments: Dict[str, Any]) -> Dict[str, Any]:
    questions = {}
    for question in arguments["questions"]:
        name = question["name"]
        if name in questions:
            raise _invalid()
        normalized = {"type": question["type"], "instructions": _structured(question["instructions"])}
        if question["type"] == "boolean":
            if question["criteria"] is not None:
                normalized["criteria"] = {key: value for key, value in question["criteria"].items() if value is not None}
        elif question["type"] == "choice":
            criteria = {}
            for option in question["options"]:
                if option["label"] in criteria:
                    raise _invalid()
                criteria[option["label"]] = option["description"]
            normalized["criteria"] = criteria
        else:
            normalized["criteria"] = question["options"]
        questions[name] = normalized
    request = {
        "state": _structured(arguments["state"]), "questions": questions,
        "model": arguments["model"] if arguments["model"] is not None else "system-zero",
    }
    if arguments["attachments"] is not None:
        request["attachments"] = arguments["attachments"]
    return request


def _canonical(value: Any) -> bytes:
    """The shared language-neutral fingerprint uses UTF-8 and IEEE754 numbers."""
    if value is None:
        return b"n"
    if type(value) is bool:
        return b"b1" if value else b"b0"
    if type(value) is str:
        encoded = value.encode("utf-8")
        return b"s" + str(len(encoded)).encode("ascii") + b":" + encoded
    if type(value) in (int, float):
        number = float(value) if value != 0 else 0.0
        if not math.isfinite(number):
            raise _invalid()
        return b"d" + struct.pack(">d", number).hex().encode("ascii")
    if type(value) is list:
        return b"a" + str(len(value)).encode("ascii") + b":[" + b"".join(_canonical(item) for item in value) + b"]"
    if type(value) is dict:
        keys = sorted(value, key=lambda key: key.encode("utf-8"))
        return b"o" + str(len(keys)).encode("ascii") + b":[" + b"".join(
            _canonical(key) + _canonical(value[key]) for key in keys
        ) + b"]"
    raise _invalid()


class ToolHandler:
    """One host-owned call limit and idempotency namespace for an agent's tool loop.

    A client supplied by the host stays host-owned. Otherwise this handler creates
    and closes an SDK client whose key comes only from SYSTEM_ZERO_API_KEY.
    """

    def __init__(
        self, client: Optional[SystemZero] = None, *, namespace: str = "system-zero-tools-v1",
        max_decide_calls: int = 100, base_url: Optional[str] = None,
    ):
        bounds = (max_decide_calls,)
        if any(type(value) not in (int, float) or not 0 <= value <= _MAX_SAFE_INTEGER or
               not math.isfinite(value) or int(value) != value for value in bounds):
            raise _invalid()
        if type(namespace) is not str or not namespace:
            raise _invalid()
        try:
            namespace.encode("utf-8")
        except UnicodeError:
            raise _invalid() from None
        if base_url is not None:
            _checked_base_url(base_url)
        self._client = client
        self._owns_client = client is None
        self._namespace = namespace
        self._base_url = base_url
        self._max_decide_calls = int(max_decide_calls)
        self._decide_calls = 0
        self._lock = Lock()
        self._closed = False

    def __enter__(self) -> "ToolHandler":
        return self

    def __exit__(self, _exc_type: Any, _exc_value: Any, _traceback: Any) -> None:
        self.close()

    def __call__(self, name: str, args: Any) -> Dict[str, Any]:
        return self.handle_tool_call(name, args)

    @property
    def budget(self) -> Dict[str, int]:
        with self._lock:
            return {"decide_calls": self._decide_calls}

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                if self._owns_client and self._client is not None:
                    self._client.close()

    def _sdk(self) -> SystemZero:
        with self._lock:
            if self._closed:
                raise SystemZeroError(_ERRORS["system_zero_unavailable"]["message"], code="system_zero_unavailable")
            if self._client is None:
                self._client = SystemZero(**({"base_url": self._base_url} if self._base_url is not None else {}))
            return self._client

    def _error(self, code: str, *, message: Optional[str] = None, request_id: Optional[str] = None) -> Dict[str, Any]:
        if code not in _ERRORS:
            code, message = "system_zero_unavailable", None
        text = message if type(message) is str else _ERRORS[code]["message"]
        return {
            "status": "held" if code.endswith("_not_available") else "error",
            "request_id": request_id if type(request_id) is str else None,
            "error": {"code": code, "message": text},
        }

    def _clean_result(self, value: Any) -> Any:
        if isinstance(value, str):
            keys = (getattr(self._client, "_api_key", None), os.environ.get("SYSTEM_ZERO_API_KEY"))
            for key in keys:
                if isinstance(key, str) and key:
                    value = value.replace(key, "[redacted]")
            value = re.sub(r"\brnsk_[A-Za-z0-9_-]{20,}\b", "[redacted]", value)
            return re.sub(r"\bBearer[ \t]+[A-Za-z0-9._~+/-]+=*", "Bearer [redacted]", value, flags=re.IGNORECASE)
        if isinstance(value, list):
            return [self._clean_result(item) for item in value]
        if isinstance(value, dict):
            return {self._clean_result(key): self._clean_result(item) for key, item in value.items()}
        return value

    def handle_tool_call(self, name: str, args: Any) -> Dict[str, Any]:
        return self._clean_result(self._invoke(name, args))

    def _invoke(self, name: str, args: Any) -> Dict[str, Any]:
        try:
            arguments = _arguments(name, args)
            request = _request(arguments) if name == "system_zero_decide" else None
            key = "sz-tools-" + hashlib.sha256(_canonical([self._namespace, request])).hexdigest() if request is not None else None
        except (SystemZeroError, ValueError, TypeError, OverflowError, RecursionError):
            return self._error("invalid_request")
        if request is not None:
            with self._lock:
                if self._decide_calls >= self._max_decide_calls:
                    return {"status": "error", "request_id": None,
                            "error": {"code": "call_limit_reached", "message": "The local call limit has been reached."}}
        try:
            client = self._sdk()
            if request is not None:
                with self._lock:
                    # Another caller may have used the last slot during client creation.
                    if self._decide_calls >= self._max_decide_calls:
                        return {"status": "error", "request_id": None,
                                "error": {"code": "call_limit_reached", "message": "The local call limit has been reached."}}
                    self._decide_calls += 1
            token = _retry_context.set({"workflow_recovery": True})
            try:
                response = client.decide_with_response(request, idempotency_key=key) if request is not None else client.models.list_with_response()
            finally:
                _retry_context.reset(token)
            data = response.data
            request_id = response.metadata.request_id or data.get("id")
            result = {**data, "status": "ok", "request_id": request_id if type(request_id) is str else None}
            answers = data.get("answers")
            if type(answers) is dict and answers and all(
                type(answer) is dict and type(answer.get("error")) is dict for answer in answers.values()
            ):
                codes = []
                for name in sorted(answers, key=lambda name: name.encode("utf-8")):
                    code = answers[name]["error"].get("code")
                    codes.append(code if type(code) is str and code in _ERRORS else "system_zero_unavailable")
                code = next((code for code in codes if not code.endswith("_not_available")), codes[0])
                result.update(self._error(code, request_id=request_id))
            return result
        except SystemZeroError as error:
            if type(error.code) is str and error.code in _ERRORS:
                return self._error(error.code, message=error.message, request_id=error.request_id)
            return self._error("system_zero_unavailable")
        except Exception:
            # Exception text may contain request data, URLs or authentication.
            return self._error("system_zero_unavailable")


def create_tool_handler(
    client: Optional[SystemZero] = None, *, namespace: str = "system-zero-tools-v1",
    max_decide_calls: int = 100, base_url: Optional[str] = None,
) -> ToolHandler:
    return ToolHandler(
        client, namespace=namespace, max_decide_calls=max_decide_calls,
        base_url=base_url,
    )


def handle_tool_call(name: str, args: Any) -> Dict[str, Any]:
    """Use the factory to share a call limit across a complete agent tool loop."""
    with create_tool_handler(max_decide_calls=0) as handler:
        return handler(name, args)


__all__ = [
    "ANTHROPIC_TOOLS", "OPENAI_TOOLS", "OPENAI_RESPONSES_TOOLS",
    "ToolHandler", "create_tool_handler", "handle_tool_call",
]
