from copy import deepcopy
import ast
import json
import math
from pathlib import Path
import struct

import pytest

from system_zero import Decision, QuestionRefusal, decision_for
from system_zero import decisions as helpers


_FIXTURE_DIRECTORY = Path(__file__).resolve().parents[2] / "fixtures" / "decision-reader"
_READER_FIXTURES = [
    case
    for path in sorted(_FIXTURE_DIRECTORY.glob("*.json"))
    for case in json.loads(path.read_text(encoding="utf-8"))
]
assert _READER_FIXTURES, "The shared decision-reader fixtures must be present."


_METADATA_FIXTURES = [case for case in _READER_FIXTURES if "metadata" in case]


@pytest.mark.parametrize("case", _METADATA_FIXTURES, ids=lambda case: case["name"])
def test_shared_decision_metadata(case):
    answer = deepcopy(case["answer"])
    before = deepcopy(answer)
    for result in (decision_for(answer), helpers.decisions_for({"answers": {"sample": answer}})["sample"]):
        assert isinstance(result, Decision)
        assert result.preview is case["metadata"].get("preview")
        assert result.calibration_version == case["metadata"].get("calibration_version")
    assert answer == before


def test_mixed_answers_and_refusal_keep_metadata_local():
    answers = {case["name"]: deepcopy(case["answer"]) for case in _METADATA_FIXTURES}
    before = deepcopy(answers)
    results = helpers.decisions_for({"answers": answers})
    assert list(results) == [case["name"] for case in _METADATA_FIXTURES]
    for case in _METADATA_FIXTURES:
        result = results[case["name"]]
        assert result.preview is case["metadata"].get("preview")
        assert result.calibration_version == case["metadata"].get("calibration_version")
    assert answers == before


@pytest.mark.parametrize("neighbor", [entry for case in _READER_FIXTURES for entry in case.get("ieeeNeighbors", [])], ids=lambda entry: entry["from"] + "-" + entry["to"])
def test_shared_ieee_probability_neighbor(neighbor):
    value = struct.unpack(">d", bytes.fromhex(neighbor["from"]))[0]
    adjacent = math.nextafter(value, math.inf if neighbor["increasing"] else -math.inf)
    assert struct.pack(">d", adjacent).hex() == neighbor["to"]


@pytest.mark.parametrize("bound", [entry for case in _READER_FIXTURES for entry in case.get("rawProbabilityBounds", [])], ids=lambda entry: str(entry["units"]) + "-" + str(entry["increasing"]))
def test_shared_raw_probability_endpoint_bits(bound):
    actual = helpers._raw_probability_bound(bound["units"], bound["increasing"])
    assert struct.pack(">d", actual).hex() == bound["bits"]


def _score_work_reader(budget=19_000, public=False):
    """Count private search operations without adding an SDK export or callback."""
    counts = {"popped": 0, "boundary": 0}

    def tick(kind):
        # The old implementation fails deterministically instead of hanging this test.
        assert sum(counts.values()) < budget, "score search exceeded its work budget"
        counts[kind] += 1

    class CountWork(ast.NodeTransformer):
        def __init__(self):
            self.functions = []

        def visit_FunctionDef(self, node):
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()
            return node

        def visit_Assign(self, node):
            kind = None
            if self.functions[-1] == "boundary" and any(isinstance(target, ast.Name) and target.id == "middle" for target in node.targets):
                kind = "boundary"
            if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute) and isinstance(node.value.func.value, ast.Name) and node.value.func.value.id == "pending" and node.value.func.attr == "pop":
                kind = "popped"
            return [ast.parse("tick(" + repr(kind) + ")").body[0], node] if kind else node

    source = ast.parse(Path(helpers.__file__).read_text(encoding="utf-8"))
    source.body = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in ("_raw_probability_bound", "_score_witness", "_expected_score", "decision_for", "decisions_for")]
    namespace = {**vars(helpers), "tick": tick, "_SCORE_SEARCH_BUDGET": budget}
    exec(compile(ast.fix_missing_locations(CountWork().visit(source)), str(helpers.__file__), "exec"), namespace)
    return namespace if public else namespace["_expected_score"], counts


@pytest.mark.parametrize("budget", [0, 1, 2, 127])
@pytest.mark.parametrize("score,last", [(4.500010000025178, 100_001), (4.499989999974824, 99_999)])
def test_score_search_budget_accepts_inside_envelope(budget, score, last):
    read, counts = _score_work_reader(budget)
    levels = {str(index): 100_000 if index < 9 else last for index in range(10)}
    assert read(score, levels) is True
    assert sum(counts.values()) == budget
    if budget == 1:
        assert counts == {"popped": 1, "boundary": 0}
    elif budget == 2:
        assert counts == {"popped": 2, "boundary": 0}
    elif budget == 127:
        assert counts["boundary"] > 0


@pytest.mark.parametrize("score,last", [(4.500010000025179, 100_001), (4.499989999974823, 99_999)])
def test_score_search_budget_never_admits_outside_envelope(score, last):
    read, counts = _score_work_reader(0)
    levels = {str(index): 100_000 if index < 9 else last for index in range(10)}
    assert read(score, levels) is False
    assert counts == {"popped": 0, "boundary": 0}


def test_score_search_budget_has_three_times_largest_completed_legitimate_search():
    read, counts = _score_work_reader()
    levels = {str(index): 750_000 if index == 0 else 250_001 if index == 9 else 0 for index in range(10)}
    assert read(2.2500100000089986, levels) is True
    assert counts == {"popped": 15, "boundary": 6162}
    assert helpers._SCORE_SEARCH_BUDGET == 19_000
    # Exhaustion accepts only inside the envelope, so headroom cannot reject a legitimate reply.
    assert 3 * 6177 <= helpers._SCORE_SEARCH_BUDGET


def test_score_search_budget_resets_for_each_answer():
    read, counts = _score_work_reader(127)
    levels = {str(index): 100_000 if index < 9 else 100_001 for index in range(10)}
    for _ in range(3):
        counts.update(popped=0, boundary=0)
        assert read(4.500010000025178, levels) is True
        assert sum(counts.values()) == 127


@pytest.mark.parametrize("case", [case for case in _READER_FIXTURES if "search" in case], ids=lambda case: case["name"])
def test_score_search_shared_fixture_work_bound(case):
    read, counts = _score_work_reader()
    answer = case["answer"]
    values = {key: helpers._units(value) for key, value in answer["probabilities"].items()}
    assert len(values) == 10
    assert read(answer["score"], values) is case["accepts"]
    assert sum(counts.values()) <= helpers._SCORE_SEARCH_BUDGET
    search = case["search"]
    if search["exhausts"]:
        assert case["accepts"] is search["inEnvelope"] is True
        assert sum(counts.values()) == helpers._SCORE_SEARCH_BUDGET
    else:
        assert counts == {"popped": search["popped"], "boundary": search["boundary"]}
    if not search["inEnvelope"]:
        assert case["accepts"] is False
        assert counts == {"popped": 0, "boundary": 0}


@pytest.mark.parametrize("field", ["confidence", "decision", "alternatives", "legend", "probabilities"])
def test_score_search_exhaustion_preserves_other_reader_validation(field):
    namespace, counts = _score_work_reader(0, public=True)
    answer = deepcopy(next(case["answer"] for case in _READER_FIXTURES if case.get("search", {}).get("exhausts")))
    assert namespace["decision_for"](answer).kind == "v1"
    assert namespace["decisions_for"]({"answers": {"sample": answer}})["sample"].kind == "v1"
    if field == "confidence":
        answer[field] = 0.1
    elif field == "decision":
        answer[field] = "0"
    elif field == "alternatives":
        answer[field] = []
    elif field == "legend":
        answer[field] = {"0": "Level 0"}
    else:
        answer[field]["9"] = 0.100001000000001
    before = deepcopy(answer)
    with pytest.raises(ValueError):
        namespace["decision_for"](answer)
    with pytest.raises(ValueError):
        namespace["decisions_for"]({"answers": {"sample": answer}})
    assert counts == {"popped": 0, "boundary": 0}
    assert answer == before


@pytest.mark.parametrize("case", _READER_FIXTURES, ids=lambda case: case["name"])
def test_shared_decision_reader_fixture(case):
    answer = deepcopy(case["answer"])
    original = deepcopy(answer)
    reply = {"answers": {"sample": answer}}
    original_reply = deepcopy(reply)
    # A removed production bound must fail these long-search fixtures, never hang.
    if case.get("search", {}).get("exhausts"):
        namespace, counts = _score_work_reader(public=True)
        read_answer, read_reply = namespace["decision_for"], namespace["decisions_for"]
    else:
        read_answer, read_reply = decision_for, helpers.decisions_for
        counts = {}

    if not case["accepts"]:
        with pytest.raises(ValueError):
            read_answer(answer)
        assert answer == original
        with pytest.raises(ValueError):
            read_reply(reply)
        assert reply == original_reply
        return

    direct = read_answer(answer)
    assert answer == original
    counts.update(popped=0, boundary=0)
    results = read_reply(reply)
    assert list(results) == ["sample"]
    assert results["sample"] == direct
    assert reply == original_reply

    for result in (direct, results["sample"]):
        assert result.kind == case.get("kind", "v1")
        if result.kind == "v1":
            assert type(result.decision) is type(answer["decision"])
            assert result.decision == answer["decision"]
            assert result.confidence == result.probability == answer["confidence"]
            assert result.alternatives is answer["alternatives"]
            assert result.calibrated is answer["calibrated"]
            assert result.refusal is None
        elif result.kind == "legacy":
            assert result.raw is answer
            assert result.confidence is result.probability is result.calibrated is None
            assert result.alternatives == []
            assert result.refusal is None
        else:
            assert isinstance(result.refusal, QuestionRefusal)
            assert result.refusal.code == answer["error"]["code"]
            assert result.refusal.retryable is False
            assert result.decision is result.confidence is result.probability is result.calibrated is None
            assert result.alternatives == []


def boolean_v1():
    return {
        "type": "boolean", "probability": 0.06, "decision": False, "confidence": 0.94,
        "alternatives": [{"decision": True, "confidence": 0.06}],
        "calibrated": False, "calibration_version": "synthetic/not-attested",
    }


def choice_v1():
    return {
        "type": "choice", "choice": "chosen", "decision": "chosen",
        "probabilities": {"chosen": 0.6, "b": 0.2, "a": 0.2, "zero": 0}, "confidence": 0.6,
        "alternatives": [{"decision": "a", "confidence": 0.2}, {"decision": "b", "confidence": 0.2}, {"decision": "zero", "confidence": 0}],
        "calibrated": True, "calibration_version": "synthetic/not-attested",
    }


def score_v1():
    return {
        "type": "score", "score": 1.1, "legend": {"0": "low", "1": "mid", "2": "high"},
        "probabilities": {"0": 0.1, "1": 0.7, "2": 0.2}, "decision": "1", "confidence": 0.7,
        "alternatives": [{"decision": "2", "confidence": 0.2}, {"decision": "0", "confidence": 0.1}],
        "calibrated": False, "calibration_version": "synthetic/not-attested",
    }


def legacy_boolean(probability=0.06):
    return {"type": "boolean", "probability": probability, "confidence": 0.01, "calibration_version": "synthetic/legacy"}


def test_v1_boolean_exposes_selected_confidence_and_preserves_alternatives():
    answer = boolean_v1()
    result = decision_for(answer)
    assert result.kind == "v1"
    assert result.decision is False
    assert result.confidence == result.probability == 0.94
    assert result.calibrated is False
    assert result.alternatives is answer["alternatives"]
    assert result.alternatives == [{"decision": True, "confidence": 0.06}]


def test_boolean_exact_tie_selects_true():
    result = decision_for({**boolean_v1(), "probability": 0.5, "decision": True, "confidence": 0.5, "alternatives": [{"decision": False, "confidence": 0.5}]})
    assert result.decision is True
    assert result.confidence == 0.5


def test_choice_preserves_server_selected_tied_maximum():
    result = decision_for({**choice_v1(), "choice": "b", "decision": "b", "probabilities": {"a": 0.5, "b": 0.5}, "confidence": 0.5, "alternatives": [{"decision": "a", "confidence": 0.5}]})
    assert result.decision == "b"
    assert result.confidence == 0.5


def test_choice_preserves_all_zero_probability_alternatives_and_ignores_request_order():
    answer = choice_v1()
    result = decision_for(answer, {"type": "choice", "criteria": {"chosen": None, "zero": None, "b": None, "a": None}})
    assert result.alternatives is answer["alternatives"]
    assert result.calibrated is True


def test_choice_ties_use_utf8_byte_order():
    answer = {
        **choice_v1(), "probabilities": {"chosen": 0.6, "\U00010000": 0.2, "\ue000": 0.2},
        "alternatives": [{"decision": "\ue000", "confidence": 0.2}, {"decision": "\U00010000", "confidence": 0.2}],
    }
    assert decision_for(answer).alternatives is answer["alternatives"]
    with pytest.raises(ValueError, match="[Aa]lternative"):
        decision_for({**answer, "alternatives": list(reversed(answer["alternatives"]))})


def test_one_option_choice_has_no_alternatives():
    result = decision_for({**choice_v1(), "probabilities": {"chosen": 1}, "confidence": 1, "alternatives": []})
    assert result.confidence == 1
    assert result.alternatives == []


def test_score_decision_is_level_key_not_expected_index_or_label():
    result = decision_for(score_v1())
    assert result.kind == "v1"
    assert result.decision == "1"
    assert result.confidence == result.probability == 0.7


def test_score_tie_selects_lowest_numeric_level():
    result = decision_for({**score_v1(), "score": 0.8, "probabilities": {"0": 0.4, "1": 0.4, "2": 0.2}, "decision": "0", "confidence": 0.4, "alternatives": [{"decision": "1", "confidence": 0.4}, {"decision": "2", "confidence": 0.2}]})
    assert result.decision == "0"
    assert result.confidence == 0.4


def test_score_alternative_ties_use_numeric_level_order():
    probabilities = {str(index): 0.6 if index == 1 else 0.2 if index in (2, 10) else 0 for index in range(11)}
    legend = {key: "level " + key for key in probabilities}
    alternatives = [{"decision": key, "confidence": probabilities[key]} for key in ["2", "10", "0", "3", "4", "5", "6", "7", "8", "9"]]
    assert decision_for({**score_v1(), "score": 3, "probabilities": probabilities, "legend": legend, "confidence": 0.6, "alternatives": alternatives}).alternatives is alternatives


@pytest.mark.parametrize("field", ["decision", "confidence", "alternatives", "calibrated", "calibration_version", "probability"])
def test_rejects_v1_missing_required_field(field):
    answer = boolean_v1()
    del answer[field]
    with pytest.raises(ValueError, match="[Ii]nvalid|[Ii]ncomplete"):
        decision_for(answer)


@pytest.mark.parametrize("patch", [
    {"decision": "false"}, {"decision": True}, {"decision": 0}, {"calibrated": None}, {"calibrated": 1},
    {"confidence": -0.1}, {"confidence": 1.1}, {"confidence": float("nan")}, {"confidence": float("inf")},
    {"confidence": 0.88}, {"confidence": True}, {"calibration_version": ""}, {"probability": 0.0600000001},
    {"alternatives": []}, {"alternatives": [{"decision": True, "probability": 0.06}]},
    {"alternatives": [{"decision": True, "confidence": 0.06, "reason": "SYNTHETIC"}]},
    {"alternatives": [{"decision": False, "confidence": 0.06}]},
    {"alternatives": [{"decision": True, "confidence": 0.07}]},
], ids=["string-decision", "wrong-decision", "numeric-decision", "null-calibrated", "numeric-calibrated",
        "negative-confidence", "confidence-over-one", "nan-confidence", "infinite-confidence", "confidence-margin",
        "bool-confidence", "empty-version", "off-grid-probability", "missing-alternative", "probability-alternative",
        "extra-alternative-field", "selected-alternative", "wrong-alternative-confidence"])
def test_rejects_invalid_boolean_v1(patch):
    with pytest.raises(ValueError, match="[Ii]nvalid|[Aa]lternative"):
        decision_for({**boolean_v1(), **patch})


@pytest.mark.parametrize("patch", [
    {"decision": "a"}, {"decision": 1}, {"choice": "a", "decision": "a", "confidence": 0.2},
    {"probabilities": {"chosen": 0.6, "a": 0.2}},
    {"alternatives": [{"decision": "b", "confidence": 0.2}, {"decision": "a", "confidence": 0.2}, {"decision": "zero", "confidence": 0}]},
    {"alternatives": [{"decision": "a", "confidence": 0.2}, {"decision": "b", "confidence": 0.2}]},
    {"alternatives": [{"decision": "a", "confidence": 0.2}, {"decision": "a", "confidence": 0.2}, {"decision": "zero", "confidence": 0}]},
], ids=["mismatched-choice", "numeric-decision", "not-maximum", "wrong-total", "unsorted", "missing-zero", "duplicate"])
def test_rejects_invalid_choice_v1(patch):
    with pytest.raises(ValueError, match="[Ii]nvalid|[Aa]lternative"):
        decision_for({**choice_v1(), **patch})


@pytest.mark.parametrize("patch", [
    {"decision": 1}, {"decision": "mid"}, {"decision": "1.1"}, {"legend": {"1": "mid"}}, {"score": 1.2},
    {"score": 0.8, "probabilities": {"0": 0.4, "1": 0.4, "2": 0.2}, "decision": "1", "confidence": 0.4},
], ids=["numeric-decision", "label-decision", "expected-index-decision", "incomplete-legend", "wrong-score", "wrong-tie-selection"])
def test_rejects_invalid_score_v1(patch):
    with pytest.raises(ValueError, match="[Ii]nvalid|[Aa]lternative"):
        decision_for({**score_v1(), **patch})


@pytest.mark.parametrize("probability", [0, 0.2, 0.499, 0.5, 0.9, 1])
def test_legacy_replay_is_distinct_without_confidence_probability(probability):
    answer = legacy_boolean(probability)
    result = decision_for(answer)
    assert result.kind == "legacy"
    assert result.decision is (probability >= 0.5)
    assert result.confidence is result.probability is result.calibrated is None
    assert result.alternatives == []
    assert result.raw is answer


def test_legacy_choice_preserves_selected_option():
    answer = {"type": "choice", "choice": "b", "probabilities": {"a": 0.5, "b": 0.5}, "confidence": 0, "calibration_version": "synthetic/legacy"}
    result = decision_for(answer)
    assert result.kind == "legacy"
    assert result.decision == "b"
    assert result.confidence is result.probability is None


def test_legacy_score_derives_only_selected_numeric_level():
    result = decision_for({"type": "score", "score": 1, "probabilities": {"0": 0.5, "1": 0, "2": 0.5}, "legend": {"0": "low", "1": "mid", "2": "high"}, "confidence": 0, "calibration_version": "synthetic/legacy"})
    assert result.kind == "legacy"
    assert result.decision == "0"
    assert result.confidence is result.probability is None


def test_legacy_score_preserves_level_keys_without_v1_index_restrictions():
    answer = {"type": "score", "score": 2.1, "probabilities": {"1": 0.1, "2": 0.7, "3": 0.2}, "legend": {"1": "low", "2": "mid", "3": "high"}, "confidence": 0.5, "calibration_version": "synthetic/legacy"}
    result = decision_for(answer)
    assert result.kind == "legacy"
    assert result.decision == "2"
    assert result.raw is answer


@pytest.mark.parametrize("patch", [{"alternatives": []}, {"calibrated": False}, {"decision": False}])
def test_legacy_with_partial_v1_marker_is_invalid(patch):
    with pytest.raises(ValueError, match="[Ii]nvalid|[Ii]ncomplete"):
        decision_for({**legacy_boolean(), **patch})


def test_refusal_is_typed_without_decision_fields():
    result = decision_for({"error": {"code": "future_refusal", "retryable": False}})
    assert result.kind == "refusal"
    assert result.decision is result.confidence is result.probability is result.calibrated is None
    assert result.alternatives == []
    assert isinstance(result.refusal, QuestionRefusal)
    assert result.refusal.code == "future_refusal"
    assert result.refusal.retryable is False


def test_refusal_with_decision_fields_is_invalid():
    with pytest.raises(ValueError, match="[Ii]nvalid"):
        decision_for({"error": {"code": "future_refusal", "retryable": False}, **boolean_v1()})


def test_reply_rejects_mixed_legacy_and_v1_successes():
    with pytest.raises(ValueError, match="mix|version"):
        helpers.decisions_for({"answers": {"old": legacy_boolean(), "fresh": boolean_v1()}})


@pytest.mark.parametrize("kind", ["legacy", "v1"])
def test_reply_accepts_refusals_alongside_uniform_successes(kind):
    result = helpers.decisions_for({"answers": {"valid": boolean_v1() if kind == "v1" else legacy_boolean(), "refused": {"error": {"code": "future_refusal", "retryable": False}}}})
    assert result["valid"].kind == kind
    assert isinstance(result["refused"].refusal, QuestionRefusal)
