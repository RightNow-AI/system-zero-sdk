"""Released policy validation, normally charged abstentions and retained held errors."""

from decimal import Decimal
import math
from typing import Any, Dict, Literal, Mapping, NoReturn, Optional, TypeAlias, TypeVar, Union, cast

from .decisions import Decision, LegacyDecision, PolicyAbstention, _expected_score, decision_for
from .provenance import ProvenanceReplyFields
from .errors import (
    POLICY_ERROR_CLASSES, InvalidRequestError, PolicyConfidenceFailedError,
    PolicyMaxCostExceededError, SystemZeroError, error_class,
)
from .types import (
    AbstainedAnswer, AbstainedBooleanAnswer, AbstainedChoiceAnswer, AbstainedScoreAnswer,
    AbstentionReason, Answer, DecideRequest, DecisionContext, Policy, QuestionRefusal, Usage,
)


PolicyDecideRequest: TypeAlias = DecideRequest
PolicyAnswerOrRefusal: TypeAlias = Union[Answer, QuestionRefusal]


class _PolicyReplyOptional(ProvenanceReplyFields, total=False):
    policy: Policy


class PolicyReply(_PolicyReplyOptional):
    id: str
    model: str
    served_by: str
    created: int
    context: DecisionContext
    usage: Usage
    answers: Dict[str, PolicyAnswerOrRefusal]


PolicyDecisionResult: TypeAlias = Union[Decision, LegacyDecision, PolicyAbstention]
_MEMBERS = frozenset(("min_confidence", "on_low_confidence", "require_calibrated", "max_cost_usd"))
_MAX_SAFE_INTEGER = 9007199254740991
_Request = TypeVar("_Request")


def _invalid_policy(param: str, detail: str) -> NoReturn:
    raise InvalidRequestError(
        "Invalid policy: " + detail, status=400, type="invalid_request_error",
        code="invalid_request", param=param, retryable=False,
    )


def _finite_number(value: Any) -> bool:
    # Bounds precede float conversion so arbitrary-size Python ints are refused
    # as input errors, rather than raising OverflowError inside math.isfinite.
    return type(value) in (int, float) and -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER and math.isfinite(value)


def policy_max_cost_microcents(value: Any) -> int:
    """Exact microcents for the decimal number that this SDK serializes."""
    def invalid() -> NoReturn:
        _invalid_policy("policy.max_cost_usd", "max_cost_usd must be a finite number greater than zero, at most 1000, with at most eight fractional digits.")

    if not _finite_number(value) or value <= 0 or value > 1000:
        invalid()
    # Decimal construction and tuple extraction are exact, regardless of the
    # caller's current decimal precision. Integer arithmetic performs scaling.
    decimal = Decimal(str(value)).as_tuple()
    microcents = 0
    for digit in decimal.digits:
        microcents = microcents * 10 + digit
    shift = 8 + cast(int, decimal.exponent)
    if shift >= 0:
        microcents *= 10 ** shift
    else:
        microcents, remainder = divmod(microcents, 10 ** (-shift))
        if remainder:
            invalid()
    if not 1 <= microcents <= 100_000_000_000:
        invalid()
    return microcents


def validate_policy(value: Any) -> Policy:
    """Validate policy only, preserving explicit members and gateway defaults."""
    if not isinstance(value, Mapping):
        _invalid_policy("policy", "policy must be an object.")
    for name in value:
        if not isinstance(name, str) or name not in _MEMBERS:
            _invalid_policy("policy.*", "unknown member.")
    if "min_confidence" in value:
        confidence = value["min_confidence"]
        if not _finite_number(confidence) or not 0 < confidence <= 1:
            _invalid_policy("policy.min_confidence", "min_confidence must be a finite number greater than zero and at most one.")
    if "on_low_confidence" in value and value["on_low_confidence"] != "abstain":
        _invalid_policy("policy.on_low_confidence", "Only abstain is available; error mode is held.")
    if "require_calibrated" in value:
        _invalid_policy("policy.require_calibrated", "require_calibrated is held until calibration is available.")
    if "max_cost_usd" in value:
        policy_max_cost_microcents(value["max_cost_usd"])
    return cast(Policy, dict(value))


def validate_request_policy(request: _Request) -> _Request:
    """Integration calls this before HTTP serialization or stream state capture."""
    if isinstance(request, Mapping) and "policy" in request:
        validate_policy(request["policy"])
    return request


def _invalid_answer(detail: str) -> NoReturn:
    raise ValueError("Invalid decision answer: " + detail)


_MILLION = 1_000_000


def _units(value: Any) -> int:
    if type(value) not in (int, float) or not 0 <= value <= 1 or not math.isfinite(value):
        _invalid_answer("probability must be a finite number in [0, 1]")
    integer = math.floor(value * _MILLION + 0.5)
    if value != integer / _MILLION:
        _invalid_answer("probability must use released millionths")
    return integer


def _abstained_fields(answer: Mapping[str, Any]):
    version = answer.get("calibration_version")
    if type(answer.get("calibrated")) is not bool or not isinstance(version, str) or not 1 <= len(version) <= 200:
        _invalid_answer("calibrated or calibration_version")
    if answer.get("type") == "boolean":
        amount = _units(answer.get("probability"))
        confidence = max(amount, _MILLION - amount) / _MILLION
        expected = [{"decision": amount < _MILLION / 2, "confidence": min(amount, _MILLION - amount) / _MILLION}]
    elif answer.get("type") in ("choice", "score"):
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, Mapping) or not probabilities or any(not isinstance(key, str) for key in probabilities):
            _invalid_answer("missing probabilities")
        values = {key: _units(value) for key, value in probabilities.items()}
        total = sum(values.values())
        # Gateway projection preserves released millionths without normalizing.
        # Integer totals retain both tolerance edges without binary float drift.
        if abs(total - _MILLION) > 1 or (len(values) == 1 and total != _MILLION):
            _invalid_answer("probabilities must sum to one within one millionth")
        score = answer["type"] == "score"
        if score:
            if any(not key.isascii() or not key.isdecimal() for key in values):
                _invalid_answer("score level keys must be zero-based decimal indices")
            levels = sorted(values, key=int)
            if any(key != str(index) for index, key in enumerate(levels)):
                _invalid_answer("score level keys must be zero-based decimal indices")
            legend = answer.get("legend")
            if not isinstance(legend, Mapping) or set(legend) != set(values) or any(not isinstance(label, str) for label in legend.values()):
                _invalid_answer("score legend must contain every level")
            if not _finite_number(answer.get("score")):
                _invalid_answer("missing expected score")
            if not _expected_score(answer["score"], values):
                _invalid_answer("score must match the expected level index within the gateway tolerance")
        ordered = sorted(values, key=lambda key: (-values[key], int(key) if score else key.encode("utf-8")))
        selected = ordered[0] if score else answer.get("choice")
        if not isinstance(selected, str) or selected not in values or values[selected] != values[ordered[0]]:
            _invalid_answer("choice must be a selected maximum")
        confidence = values[selected] / _MILLION
        expected = [{"decision": key, "confidence": values[key] / _MILLION} for key in ordered if key != selected]
    else:
        _invalid_answer("answer type")
    if type(answer.get("confidence")) not in (int, float) or answer["confidence"] != confidence:
        _invalid_answer("confidence must match the selected outcome")
    alternatives = answer.get("alternatives")
    if not isinstance(alternatives, list) or len(alternatives) != len(expected):
        _invalid_answer("alternatives must contain every other outcome")
    for value, wanted in zip(alternatives, expected):
        if not isinstance(value, Mapping) or set(value) != {"decision", "confidence"} or type(value["decision"]) is not type(wanted["decision"]) or value["decision"] != wanted["decision"] or type(value["confidence"]) not in (int, float) or value["confidence"] != wanted["confidence"]:
            _invalid_answer("alternatives must match complete outcomes in probability and tie order")
    return confidence, alternatives, answer["calibrated"]


def policy_decision_for(answer: Mapping[str, Any], question: Optional[Mapping[str, Any]] = None) -> PolicyDecisionResult:
    """Read a gateway abstention without applying a client-side confidence gate."""
    if not isinstance(answer, Mapping) or ("abstained" not in answer and "abstention_reason" not in answer):
        return decision_for(answer, question)
    reason = answer.get("abstention_reason")
    if answer.get("abstained") is not True or reason not in ("low_confidence", "uncalibrated"):
        _invalid_answer("abstained and abstention_reason must form a complete policy result")
    if "decision" in answer or "error" in answer:
        _invalid_answer("an abstention cannot contain a decision or refusal")
    confidence, alternatives, calibrated = _abstained_fields(answer)
    if reason == "uncalibrated" and calibrated:
        _invalid_answer("uncalibrated abstention contradicts calibrated=true")
    return PolicyAbstention(
        cast(AbstentionReason, reason), confidence, confidence, alternatives, calibrated, answer,
        preview=answer.get("preview"), calibration_version=answer["calibration_version"],
    )


def policy_decisions_for(reply: Mapping[str, Any], request: Optional[Mapping[str, Any]] = None) -> Dict[str, PolicyDecisionResult]:
    """Abstentions count as v1; refusals can accompany either reply version."""
    answers = reply.get("answers")
    if not isinstance(answers, Mapping):
        _invalid_answer("reply answers must be an object")
    results = {}
    version = None
    for name, answer in answers.items():
        result = policy_decision_for(answer, request.get("questions", {}).get(name) if request is not None else None)
        if result.kind != "refusal":
            current = "v1" if result.kind == "abstained" else result.kind
            if version is not None and version != current:
                _invalid_answer("reply cannot mix successful answer versions")
            version = current
        results[name] = result
    return results


POLICY_ERROR_CODES = ("policy_max_cost_exceeded", "policy_confidence_failed")
PolicyErrorCode: TypeAlias = Literal["policy_max_cost_exceeded", "policy_confidence_failed"]


PolicyError: TypeAlias = Union[PolicyMaxCostExceededError, PolicyConfidenceFailedError]


def is_policy_error(error: Any) -> bool:
    return isinstance(error, (PolicyMaxCostExceededError, PolicyConfidenceFailedError))


def as_policy_error(error: SystemZeroError) -> Optional[PolicyError]:
    """Specialize HTTP or stream errors without changing envelope metadata."""
    if is_policy_error(error):
        return cast(PolicyError, error)
    cls = POLICY_ERROR_CLASSES.get(error.code) if isinstance(error.code, str) else None
    if cls is None:
        return None
    return cls(
        error.message, status=error.status, type=error.type, code=error.code,
        param=error.param, retryable=error.retryable, details=error.details,
        request_id=error.request_id, headers=error.headers,
    )


def policy_error_class(kind: str, code: Optional[str]):
    """Drop-in error_class wiring without importing policy from errors itself."""
    cls = POLICY_ERROR_CLASSES.get(code) if isinstance(code, str) else None
    return cls if cls is not None else error_class(kind, code)
