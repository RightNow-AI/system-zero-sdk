"""Public flow, batch and evaluation shapes; validation stays in their runners."""

from pathlib import Path
from typing import Callable, Dict, List, Literal, Optional, TypeAlias, TypedDict, Union

from .types import Attachment, DecideRequest, DecisionAlternative, DecisionValue, JsonValue, Question, StructuredJson, Usage


class _FlowReference(TypedDict):
    step: str
    question: str


class _FlowEquals(_FlowReference):
    op: Literal["eq"]
    value: DecisionValue


class _FlowIn(_FlowReference):
    op: Literal["in"]
    values: List[DecisionValue]


class _FlowConfidence(_FlowReference):
    op: Literal["confidence_gte", "confidence_lt"]
    value: float


FlowCondition: TypeAlias = Union[_FlowEquals, _FlowIn, _FlowConfidence]


class _FlowLiteral(TypedDict):
    literal: JsonValue


class _FlowState(TypedDict):
    state: Literal[True]


class _FlowAnswerReference(_FlowReference):
    field: Literal["decision", "confidence", "alternatives"]


class _FlowAnswer(TypedDict):
    answer: _FlowAnswerReference


class _FlowObject(TypedDict):
    object: Dict[str, "FlowTemplate"]


class _FlowArray(TypedDict):
    array: List["FlowTemplate"]


FlowTemplate: TypeAlias = Union[_FlowLiteral, _FlowState, _FlowAnswer, _FlowObject, _FlowArray]


class FlowRule(TypedDict):
    when: FlowCondition
    next: Optional[str]


class FlowNext(TypedDict):
    rules: List[FlowRule]
    default: Optional[str]


class _FlowStepOptional(TypedDict, total=False):
    depends_on: List[FlowCondition]
    state_template: FlowTemplate
    attachments: Union[List[Attachment], None]


class FlowStep(_FlowStepOptional):
    id: str
    questions: Dict[str, Question]
    next: FlowNext


class _FlowRequestOptional(TypedDict, total=False):
    attachments: Union[List[Attachment], None]
    session_id: str
    prompt_cache_key: str
    route: Literal["standard"]
    mode: Literal["standard"]


class FlowRequest(_FlowRequestOptional):
    model: str
    state: StructuredJson


class FlowDefinition(TypedDict):
    version: Literal[1]
    request: FlowRequest
    steps: List[FlowStep]


class _FlowOptionsOptional(TypedDict, total=False):
    include_content: bool
    now: Optional[Callable[[], float]]
    max_decide_calls: int


class FlowOptions(_FlowOptionsOptional):
    run_id: str
    deadline_ms: float


class _FlowAnswerTraceOptional(TypedDict, total=False):
    question: str
    decision: DecisionValue
    alternatives: List[DecisionAlternative]


class FlowAnswerTrace(_FlowAnswerTraceOptional):
    question_index: int
    kind: Literal["v1", "legacy", "refusal"]
    confidence: Optional[float]


class FlowErrorTrace(TypedDict):
    code: str
    type: str
    status: Optional[int]
    retryable: bool


class FlowBranchTrace(TypedDict):
    rule_index: Optional[int]
    next_step_index: Optional[int]
    reason: Literal["rule_matched", "default", "dependency_unmet"]


class _FlowStepTraceOptional(TypedDict, total=False):
    error: FlowErrorTrace
    step_id: str
    request: DecideRequest
    questions: Dict[str, Question]


class FlowStepTrace(_FlowStepTraceOptional):
    decide_calls: int
    step_index: int
    status: Literal["completed", "skipped", "held", "refused", "failed"]
    request_id: Optional[str]
    request_hash: Optional[str]
    idempotency_key: Optional[str]
    question_count: int
    started_ms: float
    elapsed_ms: float
    replayed: bool
    usage: Optional[Dict[str, float]]
    answers: List[FlowAnswerTrace]
    branch: Optional[FlowBranchTrace]


class FlowTrace(TypedDict):
    decide_calls: int
    version: Literal[1]
    run_hash: str
    status: Literal["completed", "refused", "held", "failed"]
    started_ms: float
    elapsed_ms: float
    steps: List[FlowStepTrace]


ExpectedAnswers: TypeAlias = Dict[str, Optional[DecisionValue]]
QuestionType: TypeAlias = Literal["boolean", "choice", "score"]


class QuestionScore(TypedDict):
    question_id: str
    type: QuestionType
    outcome: Literal["correct", "wrong", "refused"]
    expected_refusal: bool
    correct: bool
    confidence: Optional[float]
    choice_expected: Optional[int]
    choice_actual: Optional[int]
    choice_schema: Optional[str]
    code: Optional[str]


class EvaluationRecord(TypedDict):
    id: str
    outcome: Literal["completed", "held", "error", "refused"]
    questions: List[QuestionScore]
    latency_ms: float
    usage: Optional[Usage]
    code: Optional[str]


class AccuracyMetrics(TypedDict):
    questions: int
    correct: int
    wrong: int
    refused: int
    expected_answers: int
    false_refusals: int
    accuracy: Optional[float]
    false_refusal_rate: Optional[float]


class ReliabilityBin(TypedDict):
    lower: float
    upper: float
    count: int
    accuracy: Optional[float]
    mean_confidence: Optional[float]


class _EvaluationItems(TypedDict):
    total: int
    completed: int
    refused: int
    held: int
    error: int


class _ChoiceCell(TypedDict):
    expected: int
    actual: int
    count: int


class _ChoiceConfusion(TypedDict):
    question_id: str
    choice_schema: str
    counts: List[_ChoiceCell]


class _EvaluationLatency(TypedDict):
    count: int
    p50: Optional[float]
    p95: Optional[float]


class _EvaluationUsage(TypedDict):
    totals: Usage
    records_with_usage: int
    records_without_usage: int


class _EvaluationCalibration(TypedDict):
    scope: Literal["caller_data_only"]
    label: str
    method: Literal["selected_outcome_correctness"]
    count: int
    bins: List[ReliabilityBin]
    ece: float
    brier_score: float


class EvaluationReport(TypedDict):
    version: Literal[1]
    items: _EvaluationItems
    metrics: AccuracyMetrics
    by_type: Dict[QuestionType, AccuracyMetrics]
    choice_confusion: List[_ChoiceConfusion]
    latency_ms: _EvaluationLatency
    usage: _EvaluationUsage
    calibration: Optional[_EvaluationCalibration]
    records: List[EvaluationRecord]


class BatchItem(TypedDict):
    id: str
    request: DecideRequest
    expected: ExpectedAnswers


class BatchUsage(Usage, total=False):
    pass


class BatchRecord(TypedDict):
    schema: Literal[1]
    run_id: str
    id: str
    request_hash: str
    expected_hash: str
    idempotency_key: str
    admitted_at_ms: int
    target: str
    outcome: Literal["completed", "held", "error", "refused"]
    code: Optional[str]
    attempts: int
    latency_ms: float
    usage: Optional[BatchUsage]
    questions: List[QuestionScore]


class _BatchOptionsOptional(TypedDict, total=False):
    concurrency: int
    max_retries: int
    now_ms: Callable[[], float]
    sleep_ms: Callable[[float], None]
    random: Callable[[], float]
    max_decide_calls: int


class BatchOptions(_BatchOptionsOptional):
    run_id: str
    results_path: Union[str, Path]
    deadline_ms: float


class BatchResult(TypedDict):
    decide_calls: int
    records: List[BatchRecord]
    stopped: Optional[Literal["deadline", "call_limit"]]
