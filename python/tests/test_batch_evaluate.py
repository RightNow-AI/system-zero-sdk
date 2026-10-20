import copy
import json
from pathlib import Path

import pytest

from system_zero.errors import SystemZeroError
from system_zero.evaluate import (
    evaluate, report_json, report_markdown, score_refusal, score_reply, validate_expected,
)

FIXTURE = json.loads((Path(__file__).resolve().parents[2] / "fixtures" / "batch" / "evaluation.json").read_text(encoding="utf-8"))


def records():
    return [{
        "id": row["id"], "outcome": row["outcome"], "latency_ms": row["latency_ms"],
        "usage": row.get("reply", {}).get("usage"),
 "code": row["code"],
        "questions": score_reply(row["request"], row["expected"], row["reply"]) if "reply" in row else [],
    } for row in FIXTURE["cases"]]


def test_shared_hand_computed_accuracy_false_refusal_usage_cost_and_latency():
    report = evaluate(records(), bins=FIXTURE["bins"])
    for key in ("items", "metrics", "by_type", "latency_ms", "usage"):
        assert report[key] == FIXTURE["expected"][key]
    assert "cost" not in report


def test_shared_reliability_bins_ece_and_selected_outcome_brier_on_caller_data_only():
    calibration = evaluate(records(), bins=FIXTURE["bins"])["calibration"]
    assert calibration["scope"] == "caller_data_only"
    assert calibration["label"] == "Computed on the caller's data only."
    assert calibration["method"] == "selected_outcome_correctness"
    assert calibration["count"] == FIXTURE["expected"]["calibration"]["count"]
    for key in ("ece", "brier_score"):
        assert calibration[key] == pytest.approx(FIXTURE["expected"]["calibration"][key], abs=1e-12)
    for actual, wanted in zip(calibration["bins"], FIXTURE["expected"]["calibration"]["bins"]):
        for key, value in wanted.items():
            assert actual[key] is None if value is None else actual[key] == pytest.approx(value, abs=1e-12)


def test_choice_confusion_uses_indexes_and_legacy_margins_are_excluded():
    rows = records()
    assert rows[2]["questions"][0]["confidence"] is None
    confusion = evaluate(rows, bins=5)["choice_confusion"]
    assert len(confusion) == 1
    assert confusion[0]["question_id"] == "c"
    assert confusion[0]["counts"] == [{"expected": 0, "actual": 0, "count": 1}, {"expected": 0, "actual": 1, "count": 1}]
    assert confusion[0]["choice_schema"] == FIXTURE["expected"]["choice_schema"]


def test_released_millionth_exactly_on_a_reliability_bin_boundary():
    row = FIXTURE["boundary_case"]
    report = evaluate([{
        "id": "boundary", "outcome": "completed", "latency_ms": 0, "usage": row["reply"]["usage"],
        "code": None,
        "questions": score_reply(row["request"], row["expected"], row["reply"]),
    }], bins=row["bins"])
    assert report["calibration"]["bins"][row["expected_bin"]]["count"] == 1
    assert report["calibration"]["bins"][row["expected_bin"] - 1]["count"] == 0


def test_held_and_errors_are_excluded_from_accuracy_and_quality_latency():
    rows = records()
    rows[4]["questions"] = rows[0]["questions"]
    rows[5]["questions"] = rows[1]["questions"]
    report = evaluate(rows)
    assert report["metrics"]["questions"] == 10
    assert report["latency_ms"] == {"count": 4, "p50": 250, "p95": 385}
    assert report["items"]["held"] == report["items"]["error"] == 1


def test_reports_are_content_free_json_and_markdown():
    report = evaluate(records())
    rendered_json, markdown = report_json(report), report_markdown(report)
    assert json.loads(rendered_json) == report
    for output in (rendered_json, markdown):
        assert "PRIVATE_" not in output
        assert "legacy-margin" not in output
        assert "served_by" not in output
        assert "held-capability" in output
        assert "video_not_available" in output


def test_empty_metrics_and_absent_calibration_remain_null():
    report = evaluate([])
    assert report["metrics"]["accuracy"] is None
    assert report["metrics"]["false_refusal_rate"] is None
    assert report["calibration"] is None
    assert report["latency_ms"] == {"count": 0, "p50": None, "p95": None}
    assert report["usage"]["records_with_usage"] == 0
    assert "cost" not in report


def test_expected_answers_reject_missing_extra_or_wrong_typed_labels():
    row = FIXTURE["cases"][0]
    for expected in ({}, {**row["expected"], "unexpected": True}, {**row["expected"], "b": "true"},
                     {**row["expected"], "c": "PRIVATE_MISSING"}, {**row["expected"], "s": "PRIVATE_SCORE_ONE"},
                     {**row["expected"], "b": 1}):
        with pytest.raises(SystemZeroError) as caught:
            validate_expected(row["request"], expected)
        assert caught.value.code == "invalid_request"


def test_malformed_or_unknown_remote_answers_use_typed_service_unavailable():
    row = FIXTURE["cases"][0]
    missing, extra, invalid, unknown, held, operational, whole_request = [copy.deepcopy(row["reply"]) for _ in range(7)]
    del missing["answers"]["b"]
    extra["answers"]["unexpected"] = extra["answers"]["b"]
    invalid["answers"]["b"]["confidence"] = 0.4
    unknown["answers"]["r"] = {"error": {"code": "PRIVATE_UNKNOWN_ERROR", "retryable": False}}
    held["answers"]["r"] = {"error": {"code": "video_not_available", "retryable": False}}
    operational["answers"]["r"] = {"error": {"code": "invalid_request", "retryable": False}}
    whole_request["answers"]["r"] = {"error": {"code": "request_refused", "retryable": False}}
    for reply in (missing, extra, invalid, unknown, held, operational, whole_request):
        with pytest.raises(SystemZeroError) as caught:
            score_reply(row["request"], row["expected"], reply)
        assert caught.value.code == "service_unavailable"
        assert "PRIVATE_" not in str(caught.value)


def test_whole_request_semantic_refusals_reject_held_and_operational_codes():
    row = FIXTURE["cases"][1]
    scores = {score["question_id"]: score for score in score_refusal(row["request"], row["expected"], "request_refused")}
    assert scores["b"]["outcome"] == "refused" and scores["b"]["correct"] is False
    assert scores["r"]["outcome"] == "refused" and scores["r"]["correct"] is True
    for code in ("video_not_available", "invalid_request", "input_quarantined", "workspace_quarantined"):
        with pytest.raises(SystemZeroError) as caught:
            score_refusal(row["request"], row["expected"], code)
        assert caught.value.code == "service_unavailable"


def test_choice_schemas_do_not_mix_or_depend_on_insertion_order():
    row = FIXTURE["cases"][0]
    request = copy.deepcopy(row["request"])
    request["questions"]["c"]["criteria"] = {"PRIVATE_A": None, "PRIVATE_B": None}
    assert score_reply(request, row["expected"], row["reply"])[1]["choice_schema"] == records()[0]["questions"][1]["choice_schema"]
    renamed = copy.deepcopy(records()[0])
    renamed["id"] = "different-schema"
    renamed["questions"][1]["choice_schema"] = "a" * 64
    assert len(evaluate([records()[0], renamed])["choice_confusion"]) == 2


def test_invalid_metrics_duplicate_records_and_bins_are_typed_input_errors():
    row = records()[0]
    invalid_refusal = copy.deepcopy(row)
    invalid_refusal["questions"][0].update(outcome="refused", correct=False, confidence=None, code="video_not_available")
    invalid_confidence = copy.deepcopy(row)
    invalid_confidence["questions"][0]["confidence"] = 0.2900000001
    for rows in ([row, row], [{**row, "latency_ms": -1}], [{**row, "latency_ms": float("nan")}], [{**row, "usage": {**row["usage"], "input_tokens": float("nan")}}], [invalid_refusal], [invalid_confidence]):
        with pytest.raises(SystemZeroError) as caught:
            evaluate(rows)
        assert caught.value.code == "invalid_request"
    for bins in (0, 1.5, 101, True):
        with pytest.raises(SystemZeroError):
            evaluate([], bins=bins)
