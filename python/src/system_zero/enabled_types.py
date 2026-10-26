"""Generated enabled request types; the API checks bounds and availability."""
from typing import Annotated, Any, Dict, List, Literal, TypeAlias, TypedDict, Union

StructuredJson: TypeAlias = Union[str, Dict[str, Any], List[Any]]

_EnabledAttachment0Optional = TypedDict('_EnabledAttachment0Optional', {'text': str, 'mime': str}, total=False)

class EnabledAttachment0(_EnabledAttachment0Optional):
    name: str
    type: Literal['document', 'image', 'audio', 'video']
    data: str

_EnabledAttachment1Optional = TypedDict('_EnabledAttachment1Optional', {'data': str}, total=False)

class EnabledAttachment1(_EnabledAttachment1Optional):
    name: str
    type: Literal['document', 'image', 'audio', 'video']
    text: str
    mime: str

EnabledAttachment: TypeAlias = Union[EnabledAttachment0, EnabledAttachment1]

_EnabledQuestion0_criteriaOptional = TypedDict('_EnabledQuestion0_criteriaOptional', {'true': str, 'false': str}, total=False)

class EnabledQuestion0_criteria(_EnabledQuestion0_criteriaOptional):
    pass

_EnabledQuestion0Optional = TypedDict('_EnabledQuestion0Optional', {'criteria': EnabledQuestion0_criteria}, total=False)

class EnabledQuestion0(_EnabledQuestion0Optional):
    type: Literal['boolean']
    instructions: StructuredJson

class EnabledQuestion1(TypedDict):
    type: Literal['choice']
    instructions: StructuredJson
    criteria: Annotated[Dict[str, Union[str, None]], ('minProperties', 1), ('maxProperties', 255)]

class EnabledQuestion2(TypedDict):
    type: Literal['score']
    instructions: StructuredJson
    criteria: Annotated[List[str], ('minItems', 2), ('maxItems', 10)]

EnabledQuestion: TypeAlias = Union[EnabledQuestion0, EnabledQuestion1, EnabledQuestion2]

_EnabledRequestBaseOptional = TypedDict('_EnabledRequestBaseOptional', {'attachments': Union[Annotated[List[EnabledAttachment], ('maxItems', 16)], None], 'session_id': Annotated[str, ('minLength', 1), ('maxLength', 256)], 'prompt_cache_key': Annotated[str, ('minLength', 1), ('maxLength', 256)], 'route': Literal['standard', 'committee'], 'mode': Literal['standard', 'deliberate']}, total=False)

class EnabledRequestBase(_EnabledRequestBaseOptional):
    model: Annotated[str, ('minLength', 1), ('maxLength', 200)]
    state: StructuredJson
    questions: Annotated[Dict[str, EnabledQuestion], ('minProperties', 1), ('maxProperties', 1024)]

__Shape13Optional = TypedDict('__Shape13Optional', {'true': str, 'false': str}, total=False)

class _Shape13(__Shape13Optional):
    pass

_OrdinaryQuestion0Optional = TypedDict('_OrdinaryQuestion0Optional', {'criteria': _Shape13}, total=False)

class OrdinaryQuestion0(_OrdinaryQuestion0Optional):
    type: Literal['boolean']
    instructions: StructuredJson

class OrdinaryQuestion1(TypedDict):
    type: Literal['choice']
    instructions: StructuredJson
    criteria: Annotated[Dict[str, Union[str, None]], ('minProperties', 2), ('maxProperties', 10)]

class OrdinaryQuestion2(TypedDict):
    type: Literal['score']
    instructions: StructuredJson
    criteria: Annotated[List[str], ('minItems', 2), ('maxItems', 4)]

OrdinaryQuestion: TypeAlias = Union[OrdinaryQuestion0, OrdinaryQuestion1, OrdinaryQuestion2]

_MediaQuestion0_criteriaOptional = TypedDict('_MediaQuestion0_criteriaOptional', {'true': str, 'false': str}, total=False)

class MediaQuestion0_criteria(_MediaQuestion0_criteriaOptional):
    pass

_MediaQuestion0Optional = TypedDict('_MediaQuestion0Optional', {'criteria': MediaQuestion0_criteria}, total=False)

class MediaQuestion0(_MediaQuestion0Optional):
    type: Literal['boolean']
    instructions: StructuredJson

class MediaQuestion1(TypedDict):
    type: Literal['choice']
    instructions: StructuredJson
    criteria: Annotated[Dict[str, Union[str, None]], ('minProperties', 2), ('maxProperties', 26)]

class MediaQuestion2(TypedDict):
    type: Literal['score']
    instructions: StructuredJson
    criteria: Annotated[List[str], ('minItems', 2), ('maxItems', 10)]

MediaQuestion: TypeAlias = Union[MediaQuestion0, MediaQuestion1, MediaQuestion2]

__Shape26Optional = TypedDict('__Shape26Optional', {'true': str, 'false': str}, total=False)

class _Shape26(__Shape26Optional):
    pass

_LongContextQuestion0Optional = TypedDict('_LongContextQuestion0Optional', {'criteria': _Shape26}, total=False)

class LongContextQuestion0(_LongContextQuestion0Optional):
    type: Literal['boolean']
    instructions: StructuredJson

class LongContextQuestion1(TypedDict):
    type: Literal['choice']
    instructions: StructuredJson
    criteria: Annotated[Dict[str, Union[str, None]], ('minProperties', 2), ('maxProperties', 25)]

class LongContextQuestion2(TypedDict):
    type: Literal['score']
    instructions: StructuredJson
    criteria: Annotated[List[str], ('minItems', 2), ('maxItems', 4)]

LongContextQuestion: TypeAlias = Union[LongContextQuestion0, LongContextQuestion1, LongContextQuestion2]

__Shape3201ItemOptional = TypedDict('__Shape3201ItemOptional', {'type': Literal['video']}, total=False)

class _Shape3201Item(__Shape3201ItemOptional):
    pass

_OrdinaryRequestOptional = TypedDict('_OrdinaryRequestOptional', {'questions': Annotated[Dict[str, OrdinaryQuestion], ('minProperties', 1), ('maxProperties', 1024)], 'attachments': Union[Union[List[Any], Annotated[List[_Shape3201Item], ('maxItems', 1)]], None]}, total=False)

class OrdinaryRequest(_OrdinaryRequestOptional):
    pass

_MediaRequestOptional = TypedDict('_MediaRequestOptional', {'questions': Annotated[Dict[str, MediaQuestion], ('minProperties', 1), ('maxProperties', 1024)]}, total=False)

class MediaRequest(_MediaRequestOptional):
    attachments: List[Any]

__Shape350ItemOptional = TypedDict('__Shape350ItemOptional', {'data': str, 'text': str, 'mime': str}, total=False)

class _Shape350Item(__Shape350ItemOptional):
    name: str
    type: Literal['document', 'image', 'audio', 'video']

_LongContextRequestOptional = TypedDict('_LongContextRequestOptional', {'attachments': Union[Annotated[List[_Shape350Item], ('maxItems', 16)], None], 'session_id': Annotated[str, ('minLength', 1), ('maxLength', 256)], 'prompt_cache_key': Annotated[str, ('minLength', 1), ('maxLength', 256)], 'route': Literal['standard', 'committee'], 'mode': Literal['standard', 'deliberate']}, total=False)

class LongContextRequest(_LongContextRequestOptional):
    model: Annotated[str, ('minLength', 1), ('maxLength', 200)]
    state: StructuredJson
    questions: Annotated[Dict[str, LongContextQuestion], ('minProperties', 1), ('maxProperties', 32)]

class _Shape44_text(TypedDict):
    min: int
    max: int

class _Shape44_media(TypedDict):
    min: int
    max: int

class _Shape44_long_context(TypedDict):
    min: int
    max: int

__Shape44Optional = TypedDict('__Shape44Optional', {'long_context': _Shape44_long_context}, total=False)

class _Shape44(__Shape44Optional):
    text: _Shape44_text
    media: _Shape44_media

class _Shape45_text(TypedDict):
    min: int
    max: int

class _Shape45_media(TypedDict):
    min: int
    max: int

class _Shape45(TypedDict):
    text: _Shape45_text
    media: _Shape45_media

_LongContextLimitsOptional = TypedDict('_LongContextLimitsOptional', {'images': int, 'audio': int}, total=False)

class LongContextLimits(_LongContextLimitsOptional):
    state_tokens: int
    rendered_tokens: int
    questions: int
    question_tokens: int
    question_bytes: int
    choice_options: _Shape44
    score_levels: _Shape45
    attachments: int
    body_bytes: int
    inline_attachment_bytes: int

__Shape5101ItemOptional = TypedDict('__Shape5101ItemOptional', {'type': Literal['video']}, total=False)

class _Shape5101Item(__Shape5101ItemOptional):
    pass

__EnabledOrdinaryRequestOptional = TypedDict('__EnabledOrdinaryRequestOptional', {'attachments': Union[Union[List[Any], Annotated[List[_Shape5101Item], ('maxItems', 1)]], None], 'session_id': Annotated[str, ('minLength', 1), ('maxLength', 256)], 'prompt_cache_key': Annotated[str, ('minLength', 1), ('maxLength', 256)], 'route': Literal['standard', 'committee'], 'mode': Literal['standard', 'deliberate']}, total=False)

class _EnabledOrdinaryRequest(__EnabledOrdinaryRequestOptional):
    model: Annotated[str, ('minLength', 1), ('maxLength', 200)]
    state: StructuredJson
    questions: Annotated[Dict[str, OrdinaryQuestion], ('minProperties', 1), ('maxProperties', 1024)]

__EnabledMediaRequestOptional = TypedDict('__EnabledMediaRequestOptional', {'session_id': Annotated[str, ('minLength', 1), ('maxLength', 256)], 'prompt_cache_key': Annotated[str, ('minLength', 1), ('maxLength', 256)], 'route': Literal['standard', 'committee'], 'mode': Literal['standard', 'deliberate']}, total=False)

class _EnabledMediaRequest(__EnabledMediaRequestOptional):
    model: Annotated[str, ('minLength', 1), ('maxLength', 200)]
    state: StructuredJson
    attachments: List[Any]
    questions: Annotated[Dict[str, MediaQuestion], ('minProperties', 1), ('maxProperties', 1024)]

EnabledDecideRequest: TypeAlias = Union[_EnabledOrdinaryRequest, _EnabledMediaRequest, LongContextRequest]

class VideoAttachment(TypedDict):
    name: str
    type: Literal['video']
    data: str

_VideoRequestOptional = TypedDict('_VideoRequestOptional', {'session_id': Annotated[str, ('minLength', 1), ('maxLength', 256)], 'prompt_cache_key': Annotated[str, ('minLength', 1), ('maxLength', 256)], 'route': Literal['standard', 'committee'], 'mode': Literal['standard', 'deliberate']}, total=False)

class VideoRequest(_VideoRequestOptional):
    model: Annotated[str, ('minLength', 1), ('maxLength', 200)]
    state: StructuredJson
    attachments: List[VideoAttachment]
    questions: Dict[str, OrdinaryQuestion]
