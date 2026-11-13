"""Tool selection compiled to one ordinary choice question (sz-tools-v1)."""

import re
from typing import Any, Mapping, Sequence, Literal

from .decisions import decision_for
from .errors import APIError, InvalidRequestError, SystemZeroError


def _invalid(param: str) -> InvalidRequestError:
    return InvalidRequestError("Invalid tool selection definition.", status=400,
                               type="invalid_request_error", code="invalid_request", param=param)


def tool_questions(tools: Sequence[Mapping[str, Any]], mode: Literal["auto", "required"] = "auto") -> dict:
    """Compile names and descriptions only, preserving the caller's tool order."""
    if mode not in ("auto", "required"):
        raise _invalid("mode")
    if not isinstance(tools, (list, tuple)) or not tools:
        raise _invalid("tools")
    criteria = {}
    for index, tool in enumerate(tools):
        definition = tool.get("function", tool) if isinstance(tool, Mapping) else None
        if not isinstance(definition, Mapping):
            raise _invalid(f"tools[{index}]")
        name, description = definition.get("name"), definition.get("description")
        if (not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) is None
                or name.isdigit() or name == "none" or name in criteria):
            raise _invalid(f"tools[{index}].name")
        if "description" in definition and (not isinstance(description, str) or len(description) > 1024):
            raise _invalid(f"tools[{index}].description")
        criteria[name] = description
    if mode == "auto":
        criteria["none"] = "No listed tool should be called."
    return {"sz_tool": {"type": "choice",
                        "instructions": 'Select the one tool to call next for this state. Select "none" when no listed tool should be called.',
                        "criteria": criteria}}


def _no_decision(cause: SystemZeroError | None = None) -> APIError:
    return APIError("No decision, possibly refused.", status=cause.status if cause else 503,
                    type=cause.type if cause else "api_error", code=cause.code if cause else "service_unavailable",
                    retryable=False, request_id=cause.request_id if cause else None,
                    headers=cause.headers if cause else None,
                    idempotency_key=cause.idempotency_key if cause else None)


def select_tool(client: Any, request: Mapping[str, Any], tools: Sequence[Mapping[str, Any]],
                mode: Literal["auto", "required"] = "auto", **options: Any) -> dict:
    """Use decide and its normal retry budget; retain the original reply."""
    questions = tool_questions(tools, mode)
    names = list(questions["sz_tool"]["criteria"])
    if mode == "required" and len(names) == 1:
        return dict(decision="call", tool=names[0], confidence=None, alternatives=[], reply=None, forced=True)
    try:
        reply = client.decide({**request, "questions": questions}, **options)
    except SystemZeroError as error:
        if error.status == 503:
            raise _no_decision(error) from error
        raise
    answer = reply.get("answers", {}).get("sz_tool")
    if not isinstance(answer, Mapping) or answer.get("type") != "choice":
        raise _no_decision()
    result = decision_for(answer)
    if (result.kind not in ("v1", "legacy") or result.decision not in names
            or set(answer["probabilities"]) != set(names)):
        raise _no_decision()
    return dict(decision="none" if result.decision == "none" else "call",
                tool=None if result.decision == "none" else result.decision,
                confidence=result.confidence,
                alternatives=[dict(tool=item["decision"], confidence=item["confidence"]) for item in result.alternatives],
                reply=reply, forced=False)
