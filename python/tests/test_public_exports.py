"""Public workflow imports work without a client, network or receipt files."""

import importlib
import json

import system_zero as sdk


REQUEST = {
    "model": "system-zero",
    "state": "SYNTHETIC: The review is complete.",
    "questions": {"ready": {"type": "boolean", "instructions": "Is the review complete?"}},
}


def test_public_flow_entrypoints():
    definition = {
        "version": 1,
        "request": {"model": REQUEST["model"], "state": REQUEST["state"]},
        "steps": [{"id": "review", "questions": REQUEST["questions"],
                   "next": {"rules": [], "default": None}}],
    }
    assert sdk.run_flow is importlib.import_module("system_zero.flows").run_flow
    assert sdk.validate_flow(definition) == definition
    assert sdk.flow_idempotency_key("public-flow", "review", REQUEST) == sdk.flow_idempotency_key(
        "public-flow", "review", json.loads(json.dumps(REQUEST)),
    )
    assert len(sdk.flow_request_hash(REQUEST)) == 64
    assert not hasattr(sdk, "STEP_COST_BOUND_MICROCENTS")


def test_public_batch_entrypoints():
    assert sdk.run_batch is importlib.import_module("system_zero.batch").run_batch
    assert not hasattr(sdk, "parse_budget")
    digest = sdk.request_fingerprint(REQUEST)
    assert sdk.idempotency_key("public-batch", "review", digest) == sdk.idempotency_key(
        "public-batch", "review", sdk.request_fingerprint(json.loads(json.dumps(REQUEST))),
    )
    assert not hasattr(sdk, "estimate_reserve")


def test_public_evaluation_entrypoints():
    assert callable(sdk.evaluate)
    report = sdk.evaluate([])
    assert report["items"]["total"] == 0
    assert json.loads(sdk.report_json(report)) == report
    assert "0" in sdk.report_markdown(report)
    sdk.validate_expected(REQUEST, {"ready": True})
    assert sdk.score_refusal(REQUEST, {"ready": True}, "question_not_grounded")[0]["outcome"] == "refused"
    assert callable(sdk.score_reply)
    public_names = {
        "run_flow", "validate_flow", "flow_request_hash", "flow_idempotency_key",
        "run_batch", "request_fingerprint",
        "idempotency_key", "evaluate", "report_json", "report_markdown",
        "validate_expected", "score_reply", "score_refusal",
    }
    assert public_names <= set(sdk.__all__)


def test_public_workflow_types():
    definition = sdk.FlowDefinition(
        version=1,
        request={"model": REQUEST["model"], "state": REQUEST["state"]},
        steps=[{"id": "review", "questions": REQUEST["questions"],
                "next": {"rules": [], "default": None}}],
    )
    assert sdk.validate_flow(definition) == definition
    item = sdk.BatchItem(id="review", request=REQUEST, expected={"ready": True})
    sdk.validate_expected(item["request"], item["expected"])
    record = sdk.EvaluationRecord(
        id=item["id"], outcome="held", questions=[], latency_ms=0,
        usage=None, code="video_not_available",
    )
    assert sdk.evaluate([record])["items"]["held"] == 1
    assert {"FlowDefinition", "FlowTrace", "BatchItem", "BatchResult", "EvaluationRecord",
            "EvaluationReport", "QuestionScore", "ExpectedAnswers"} <= set(sdk.__all__)
