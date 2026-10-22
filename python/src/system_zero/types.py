"""Request and response types for the System Zero API."""

from typing import Dict, List, Literal, TypeAlias, TypedDict, Union


JsonValue: TypeAlias = Union[str, int, float, bool, None, Dict[str, "JsonValue"], List["JsonValue"]]
StructuredJson: TypeAlias = Union[str, Dict[str, JsonValue], List[JsonValue]]
DecisionValue: TypeAlias = Union[bool, str]

BooleanCriteria = TypedDict("BooleanCriteria", {"true": str, "false": str}, total=False)


class _BooleanQuestionOptional(TypedDict, total=False):
    criteria: BooleanCriteria


class BooleanQuestion(_BooleanQuestionOptional):
    type: Literal["boolean"]
    instructions: StructuredJson


class ChoiceQuestion(TypedDict):
    type: Literal["choice"]
    instructions: StructuredJson
    criteria: Dict[str, Union[str, None]]


class ScoreQuestion(TypedDict):
    type: Literal["score"]
    instructions: StructuredJson
    criteria: List[str]


Question: TypeAlias = Union[BooleanQuestion, ChoiceQuestion, ScoreQuestion]


class Attachment(TypedDict):
    name: str
    type: Literal['image', 'audio']
    data: str


class _DecideRequestOptional(TypedDict, total=False):
    attachments: Union[List[Attachment], None]
    session_id: str
    prompt_cache_key: str
    route: Literal["standard"]
    mode: Literal["standard"]


class DecideRequest(_DecideRequestOptional):
    model: str
    state: StructuredJson
    questions: Dict[str, Question]


class DecisionAlternative(TypedDict):
    decision: DecisionValue
    confidence: float


class BooleanDecisionAlternative(TypedDict):
    decision: bool
    confidence: float


class StringDecisionAlternative(TypedDict):
    decision: str
    confidence: float


class _AnswerOptional(TypedDict, total=False):
    preview: Literal[True]


class _LegacyAnswerMeta(_AnswerOptional):
    confidence: float
    calibration_version: str


class LegacyBooleanAnswer(_LegacyAnswerMeta):
    """Deprecated replay: confidence is a margin, never a probability."""

    type: Literal["boolean"]
    probability: float


class LegacyChoiceAnswer(_LegacyAnswerMeta):
    """Deprecated replay: confidence is a margin, never a probability."""

    type: Literal["choice"]
    choice: str
    probabilities: Dict[str, float]


class LegacyScoreAnswer(_LegacyAnswerMeta):
    """Deprecated replay: confidence is a margin, never a probability."""

    type: Literal["score"]
    score: float
    legend: Dict[str, str]
    probabilities: Dict[str, float]


LegacyAnswer: TypeAlias = Union[LegacyBooleanAnswer, LegacyChoiceAnswer, LegacyScoreAnswer]


class _AnswerMeta(_LegacyAnswerMeta):
    calibrated: bool


class BooleanAnswer(_AnswerMeta):
    type: Literal["boolean"]
    probability: float
    decision: bool
    alternatives: List[BooleanDecisionAlternative]


class ChoiceAnswer(_AnswerMeta):
    type: Literal["choice"]
    choice: str
    probabilities: Dict[str, float]
    decision: str
    alternatives: List[StringDecisionAlternative]


class ScoreAnswer(_AnswerMeta):
    type: Literal["score"]
    score: float
    legend: Dict[str, str]
    probabilities: Dict[str, float]
    decision: str
    alternatives: List[StringDecisionAlternative]


AuthoritativeAnswer: TypeAlias = Union[BooleanAnswer, ChoiceAnswer, ScoreAnswer]


Answer: TypeAlias = Union[LegacyAnswer, AuthoritativeAnswer]


class QuestionRefusalError(TypedDict):
    code: str
    retryable: Literal[False]


class QuestionRefusal(TypedDict):
    error: QuestionRefusalError


class DecisionContext(TypedDict):
    route: Literal["direct"]
    tokens: int


class Usage(TypedDict):
    input_tokens: int
    cached_input_tokens: int
    media_tokens: int
    long_input_tokens: int
    media_items: int
    thought_tokens: int


class _DecideReplyFields(TypedDict):
    id: str
    model: str
    served_by: str
    created: int
    context: DecisionContext
    usage: Usage


class V1DecideReply(_DecideReplyFields):
    answers: Dict[str, Union[AuthoritativeAnswer, QuestionRefusal]]


class LegacyDecideReply(_DecideReplyFields):
    """Deprecated stored reply; the raw confidence fields are margins."""

    answers: Dict[str, Union[LegacyAnswer, QuestionRefusal]]


LegacyReplayReply: TypeAlias = LegacyDecideReply


DecideReply: TypeAlias = Union[V1DecideReply, LegacyDecideReply]


class ServedRange(TypedDict):
    min: int
    max: int


class TextMediaRange(TypedDict):
    text: ServedRange
    media: ServedRange


class _ModelLimitsOptional(TypedDict, total=False):
    images: int
    audio: int


class ModelLimits(_ModelLimitsOptional):
    state_tokens: int
    rendered_tokens: int
    questions: int
    question_tokens: int
    question_bytes: int
    choice_options: TextMediaRange
    score_levels: TextMediaRange
    attachments: int
    body_bytes: int
    inline_attachment_bytes: int


HeldRoute: TypeAlias = str


class Model(TypedDict):
    id: str
    object: Literal["model"]
    alias_for: Union[str, None]
    released_at: Union[str, None]
    calibration_versions: Union[List[str], None]
    limits: ModelLimits
    held_routes: List[HeldRoute]


class ModelList(TypedDict):
    object: Literal["list"]
    data: List[Model]


ErrorType: TypeAlias = Literal[
    "invalid_request_error", "authentication_error", "permission_error", "not_found_error",
    "rate_limit_error", "overloaded_error", "api_error", "server_error",
]


class _ErrorBodyOptional(TypedDict, total=False):
    details: Dict[str, JsonValue]


class ErrorBody(_ErrorBodyOptional):
    message: str
    type: ErrorType
    param: Union[str, None]
    code: Union[str, None]
    retryable: bool


class ErrorEnvelope(TypedDict):
    error: ErrorBody
    request_id: str
