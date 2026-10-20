"""Validate v1 decisions while keeping legacy replay confidence unavailable."""

from dataclasses import dataclass, field
import math
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional, Sequence, Union


DecisionValue = Union[bool, str]
_MILLION = 1_000_000
_MARKERS = frozenset(("decision", "alternatives", "calibrated"))
# Shared with TypeScript: one unit is an interval pop or a boundary midpoint step.
# Sized for about 25 ms per exhausting ten-level answer in local Python timing,
# and at least 3x the largest completed legitimate search (6,177 units).
# Exhaustion accepts only after the conservative envelope passes.
_SCORE_SEARCH_BUDGET = 19_000


@dataclass(frozen=True)
class QuestionRefusal:
    code: str
    retryable: Literal[False] = False


@dataclass(frozen=True)
class Alternative:
    decision: DecisionValue
    confidence: float

    @property
    def probability(self) -> float:
        """Alias for v1 outcome confidence."""
        return self.confidence


@dataclass(frozen=True)
class Decision:
    decision: Optional[DecisionValue]
    probability: Optional[float]
    alternatives: Sequence[Any]
    calibrated: Optional[bool]
    refusal: Optional[QuestionRefusal] = None
    confidence: Optional[float] = None
    kind: Literal["v1", "refusal"] = "v1"
    # Gateway metadata is copied verbatim; refusals retain these defaults.
    preview: Optional[Literal[True]] = None
    calibration_version: Optional[str] = None


@dataclass(frozen=True)
class LegacyDecision:
    """Stored replay: the raw confidence is a margin, never a probability."""

    decision: DecisionValue
    raw: Mapping[str, Any]
    confidence: None = None
    probability: None = None
    alternatives: Sequence[Any] = field(default_factory=list)
    calibrated: None = None
    refusal: None = None
    kind: Literal["legacy"] = "legacy"


def _invalid(detail: str):
    raise ValueError("Invalid decision answer: " + detail)


def _probability(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def _units(value: Any) -> int:
    if not _probability(value):
        _invalid("probability must be a finite number in [0, 1]")
    integer = math.floor(value * _MILLION + 0.5)
    if value != integer / _MILLION:
        _invalid("probability must use released millionths")
    return integer


def _distribution(answer: Mapping[str, Any], canonical: bool) -> Dict[str, Union[int, float]]:
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, Mapping) or not probabilities:
        _invalid("missing probabilities")
    values = {}
    for key, value in probabilities.items():
        if not isinstance(key, str) or not _probability(value):
            _invalid("probabilities must be finite numbers in [0, 1]")
        values[key] = _units(value) if canonical else value
    total = sum(values.values())
    
    if (canonical and (abs(total - _MILLION) > 1 or (len(values) == 1 and total != _MILLION))) or (not canonical and abs(total - 1) > 0.000001):
        _invalid("probabilities must sum to one")
    return values


def _score_levels(answer: Mapping[str, Any], values: Mapping[str, Any]):
    if any(not key.isascii() or not key.isdecimal() for key in values):
        _invalid("score level keys must be zero-based decimal indices")
    keys = sorted(values, key=int)
    if any(key != str(index) for index, key in enumerate(keys)):
        _invalid("score level keys must be zero-based decimal indices")
    legend = answer.get("legend")
    if not isinstance(legend, Mapping) or set(legend) != set(values) or any(not isinstance(value, str) for value in legend.values()):
        _invalid("score legend must contain every level")
    score = answer.get("score")
    if type(score) not in (int, float) or not math.isfinite(score):
        _invalid("missing expected score")


def _rank(values: Mapping[str, Union[int, float]], score: bool):
    return sorted(values, key=lambda key: (-values[key], float(key) if score else key.encode("utf-8")))


def _raw_probability_bound(value: Union[int, float], increasing: bool) -> float:
    # Invert upstream.ts's near(p * million, round(p * million)), including its float rounding.
    bound = max(0.0, min(1.0, (value + (1e-6 if increasing else -1e-6)) / _MILLION))
    direction = math.inf if increasing else -math.inf
    while abs(bound * _MILLION - value) > 1e-6:
        bound = math.nextafter(bound, -direction)
    while bound < 1 if increasing else bound > 0:
        next_bound = math.nextafter(bound, direction)
        if abs(next_bound * _MILLION - value) > 1e-6:
            break
        bound = next_bound
    return bound


def _score_witness(score: float, minimum: List[float], maximum: List[float]) -> bool:
    # Both gateway sums are monotone in each nonnegative raw probability. Narrow IEEE
    # intervals with those same predicates, then split only if rounding plateaus remain.
    # The caller already checked the conservative envelope. On exhaustion accept that
    # consistency check: the gateway is the source of truth, never SDK authorization.
    remaining = _SCORE_SEARCH_BUDGET

    def total(raw: Sequence[float], weighted: bool, index: int = -1, value: float = 0.0) -> float:
        result = 0.0
        # Keep ordered IEEE additions; hoist the mode branch out of this hot loop.
        if weighted:
            for level, probability in enumerate(raw):
                result += (value if level == index else probability) * level
        else:
            for level, probability in enumerate(raw):
                result += value if level == index else probability
        return result

    def matches(raw: Sequence[float]) -> bool:
        return abs(total(raw, False) - 1) <= 1e-6 and abs(total(raw, True) - score) <= 1e-6

    def suffix_total(raw: Sequence[float], weighted: bool, index: int, value: float, prefix: float) -> float:
        # Resume the same ordered IEEE sum. Coordinates before index stay fixed
        # during a synchronous boundary search; never subtract from a full sum.
        if weighted:
            result = prefix + value * index
            for level in range(index + 1, len(raw)):
                result += raw[level] * level
        else:
            result = prefix + value
            for level in range(index + 1, len(raw)):
                result += raw[level]
        return result

    def boundary(low: float, high: float, passes: Callable[[float], bool], first: bool) -> Optional[float]:
        nonlocal remaining
        while True:
            if remaining <= 0:
                return None
            remaining -= 1
            middle = (low + high) / 2
            if middle == low or middle == high:
                return high if first else low
            if passes(middle) == first:
                high = middle
            else:
                low = middle

    pending = [(minimum, maximum)]
    while pending:
        if remaining <= 0:
            return True
        remaining -= 1
        low, high = pending.pop()
        possible = True
        for _ in range(4):
            if not possible:
                break
            changed = False
            for weighted in (False, True):
                target = score if weighted else 1
                if total(low, weighted) - target > 1e-6 or target - total(high, weighted) > 1e-6:
                    possible = False
                    break
                for index in range(len(low)):
                    # Recompute each prefix after prior coordinates have been narrowed.
                    high_prefix = total(high[:index], weighted)

                    def above(value: float) -> bool:
                        return target - suffix_total(high, weighted, index, value, high_prefix) <= 1e-6

                    if not above(high[index]):
                        possible = False
                        break
                    if not above(low[index]):
                        first = boundary(low[index], high[index], above, True)
                        if first is None:
                            return True
                        low[index] = first
                        changed = True

                    low_prefix = total(low[:index], weighted)

                    def below(value: float) -> bool:
                        return suffix_total(low, weighted, index, value, low_prefix) - target <= 1e-6

                    if not below(low[index]):
                        possible = False
                        break
                    if not below(high[index]):
                        last = boundary(low[index], high[index], below, False)
                        if last is None:
                            return True
                        high[index] = last
                        changed = True
                if not possible:
                    break
            if not changed:
                break
        if not possible:
            continue
        if matches(low) or matches(high):
            return True
        split, width = -1, 0.0
        for index in range(len(low)):
            span = (high[index] - low[index]) * max(1, index)
            if span > width:
                split, width = index, span
        if split < 0:
            continue
        middle = (low[split] + high[split]) / 2
        if middle == high[split]:
            middle = math.nextafter(middle, -math.inf)
        left_high, right_low = high.copy(), low.copy()
        left_high[split] = middle
        right_low[split] = math.nextafter(middle, math.inf)
        pending.append((low.copy(), left_high))
        pending.append((right_low, high.copy()))
    return False


def _expected_score(score: float, values: Mapping[str, Union[int, float]]) -> bool:
    levels = [values[key] for key in sorted(values, key=int)]
    projected = [value / _MILLION for value in levels]
    projected_total = projected_score = 0.0
    for index, value in enumerate(projected):
        projected_total += value
        projected_score += value * index
    if abs(projected_total - 1) <= 1e-6 and abs(projected_score - score) <= 1e-6:
        return True
    expected = sum(index * value for index, value in enumerate(levels)) / _MILLION
    increasing = score > expected
    raw = [_raw_probability_bound(value, increasing) for value in levels]

    def within_mass_bound() -> bool:
        # Explicit sequential sums match JavaScript reduce, including on Python 3.12+.
        total = 0.0
        for value in raw:
            total += value
        return total - 1 <= 1e-6 if increasing else 1 - total <= 1e-6

    # decision-fields.ts preserves the raw score while projecting probabilities. Find an
    # attainable boundary candidate by moving the lowest levels first. Binary search keeps
    # the original mass predicate; the witness search below handles IEEE sum plateaus.
    for index in range(len(raw)):
        if within_mass_bound():
            break
        initial = raw[index]
        raw[index] = _raw_probability_bound(levels[index], not increasing)
        if not within_mass_bound():
            continue
        low, high = min(initial, raw[index]), max(initial, raw[index])
        while True:
            middle = (low + high) / 2
            if middle == low or middle == high:
                break
            raw[index] = middle
            if within_mass_bound() == increasing:
                low = middle
            else:
                high = middle
        raw[index] = low if increasing else high

    endpoint = total = 0.0
    for index, value in enumerate(raw):
        endpoint += value * index
        total += value
    if abs(total - 1) <= 1e-6 and abs(score - endpoint) <= 1e-6:
        return True
    # Reject distant misses before searching plateaus. Sequential mass error is O(n * eps),
    # its score effect is at most n times that, and weighted-sum error is O(n * n * eps).
    # This conservative gamma_n envelope must pass before a bounded search can accept
    # on exhaustion. Diagnostic consistency is never an authorization check.
    gamma = len(levels) * math.ulp(1.0) / (1 - len(levels) * math.ulp(1.0))
    if abs(score - endpoint) > 1e-6 + 8 * gamma * len(levels):
        return False
    return _score_witness(
        score,
        [_raw_probability_bound(value, False) for value in levels],
        [_raw_probability_bound(value, True) for value in levels],
    )


def _alternatives(answer: Mapping[str, Any], expected: Sequence[Mapping[str, Any]]):
    alternatives = answer["alternatives"]
    if not isinstance(alternatives, list) or len(alternatives) != len(expected):
        _invalid("alternatives must contain every other outcome")
    for value, wanted in zip(alternatives, expected):
        if not isinstance(value, Mapping) or set(value) != {"decision", "confidence"}:
            _invalid("alternatives require strict {decision, confidence} objects")
        if type(value["decision"]) is not type(wanted["decision"]) or value["decision"] != wanted["decision"] or not _probability(value["confidence"]) or value["confidence"] != wanted["confidence"]:
            _invalid("alternatives must match complete outcomes in probability and tie order")


def decision_for(answer: Mapping[str, Any], question: Optional[Mapping[str, Any]] = None) -> Union[Decision, LegacyDecision]:
    """Read complete v1 fields, or return a distinctly tagged legacy replay.

    Question is retained for call compatibility; request order never orders v1
    alternatives. The server's validated alternatives are preserved by reference.
    """
    if not isinstance(answer, Mapping):
        _invalid("answer must be an object")
    if "error" in answer:
        error = answer["error"]
        if set(answer) != {"error"} or not isinstance(error, Mapping) or set(error) != {"code", "retryable"} or not isinstance(error["code"], str) or error["retryable"] is not False:
            _invalid("question refusal shape")
        return Decision(None, None, [], None, QuestionRefusal(error["code"]), kind="refusal")
    if answer.get("type") not in ("boolean", "choice", "score"):
        _invalid("answer type")
    version = answer.get("calibration_version")
    if not _probability(answer.get("confidence")) or not isinstance(version, str) or not 1 <= len(version) <= 200:
        _invalid("confidence or calibration_version")
    present = _MARKERS.intersection(answer)
    if present and present != _MARKERS:
        _invalid("incomplete v1 fields")
    v1 = bool(present)
    if v1 and type(answer["calibrated"]) is not bool:
        _invalid("calibrated must be boolean")
    if answer["type"] == "boolean":
        if not _probability(answer.get("probability")):
            _invalid("boolean probability")
        amount = _units(answer["probability"]) if v1 else answer["probability"]
        whole = _MILLION if v1 else 1
        selected = amount >= whole / 2
        confidence = max(amount, whole - amount) / whole
        expected = [{"decision": not selected, "confidence": min(amount, whole - amount) / whole}]
    else:
        values = _distribution(answer, v1)
        score = answer["type"] == "score"
        if score and v1:
            _score_levels(answer, values)
        ordered = _rank(values, score)
        if score:
            selected = ordered[0]
            if v1 and not _expected_score(answer["score"], values):
                _invalid("score must match the expected level index within the gateway tolerance")
        else:
            selected = answer.get("choice")
            if not isinstance(selected, str) or selected not in values or values[selected] != values[ordered[0]]:
                _invalid("choice must be a selected maximum")
        confidence = values[selected] / (_MILLION if v1 else 1)
        expected = [{"decision": key, "confidence": values[key] / (_MILLION if v1 else 1)} for key in ordered if key != selected]
    if not v1:
        return LegacyDecision(selected, answer)
    if type(answer["decision"]) is not type(selected) or answer["decision"] != selected or answer["confidence"] != confidence:
        _invalid("decision and confidence must match the selected outcome")
    _alternatives(answer, expected)
    return Decision(selected, answer["confidence"], answer["alternatives"], answer["calibrated"],
                    confidence=answer["confidence"], preview=answer.get("preview"),
                    calibration_version=answer["calibration_version"])


def decisions_for(reply: Mapping[str, Any], request: Optional[Mapping[str, Any]] = None) -> Dict[str, Union[Decision, LegacyDecision]]:
    """Refusals may accompany either version; successful answer versions cannot mix."""
    answers = reply.get("answers")
    if not isinstance(answers, Mapping):
        _invalid("reply answers must be an object")
    results = {}
    version = None
    for name, answer in answers.items():
        result = decision_for(answer, request.get("questions", {}).get(name) if request is not None else None)
        if result.kind != "refusal":
            current = result.kind
            if version is not None and version != current:
                _invalid("reply cannot mix successful answer versions")
            version = current
        results[name] = result
    return results
