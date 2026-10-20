"""Declarative decision flows over the existing, synchronous decide client.

The default trace omits caller content. Each admitted dispatch consumes one
call from a finite local count cap, and every trace records that count.
"""
import hashlib
import math
import re
import struct
import time
from typing import Any, Callable, Dict, Mapping, Optional, cast
from .client import SystemZero
from ._retry import _retry_context
from .decisions import decisions_for
from .public_error_codes import KNOWN_ERROR_CODES
from .errors import APIConnectionError, APIError, APITimeoutError, InvalidRequestError, SystemZeroError, error_class
from .workflow_types import FlowDefinition, FlowTrace
MAX_FLOW_STEPS = 32
_MAX_SAFE_INTEGER = 9007199254740991
_STEP_ID = re.compile('[A-Za-z][A-Za-z0-9_-]{0,63}\\Z')
_HELD_CODES = frozenset({'long_context_not_available', 'long_recording_not_available', 'video_not_available', 'image_not_available', 'audio_not_available', 'document_not_available', 'committee_not_available', 'deliberate_not_available'})
_KNOWN_CODES = KNOWN_ERROR_CODES | {'call_limit_reached'}
_ERROR_TYPES = frozenset({'invalid_request_error', 'authentication_error', 'permission_error', 'not_found_error', 'rate_limit_error', 'overloaded_error', 'api_error', 'server_error'})
_USAGE_FIELDS = ('input_tokens', 'cached_input_tokens', 'media_tokens', 'long_input_tokens', 'media_items', 'thought_tokens')
_QUESTION_REFUSALS = {'injection_suspected': 'The request was refused because its content reads as instructions to the model. Remove the embedded instructions and send it again.', 'question_not_grounded': 'The question cannot be answered from this state.'}
_HELD_MESSAGES = {'long_context_not_available': 'This route is not available.', 'long_recording_not_available': 'This input type is not supported.', 'video_not_available': 'This input type is not supported.', 'image_not_available': 'Images are unavailable. Send the request without the image attachment.', 'audio_not_available': 'Audio is unavailable. Send the request without the audio attachment.', 'document_not_available': 'Documents are unavailable. Send the request without the document attachment.', 'committee_not_available': 'This route is not available.', 'deliberate_not_available': 'This mode is not available.'}

def _invalid() -> InvalidRequestError:
    return InvalidRequestError('The RightNow System Zero request is invalid.', status=400, type='invalid_request_error', code='invalid_request')

def _unavailable(request_id: Optional[str]=None) -> APIError:
    return APIError('RightNow System Zero cannot serve this request right now. Retry after the Retry-After delay.', status=503, type='api_error', code='service_unavailable', retryable=True, request_id=request_id)

def _timeout(request_id: Optional[str]=None) -> APITimeoutError:
    return APITimeoutError('RightNow System Zero exceeded the response deadline.', status=504, type='api_error', code='system_zero_timeout', request_id=request_id)

def _safe_integer(value: Any) -> bool:
    if type(value) is int:
        return abs(value) <= _MAX_SAFE_INTEGER
    return type(value) is float and math.isfinite(value) and value.is_integer() and (abs(value) <= _MAX_SAFE_INTEGER)

def _number(value: Any) -> bool:
    return type(value) is int or (type(value) is float and math.isfinite(value))

def _utf8(value: str) -> bytes:
    try:
        return value.encode('utf-8')
    except UnicodeError:
        raise _invalid() from None

def _snapshot(value: Any) -> Any:
    """Copy strict portable JSON without recursion or invoking user objects."""
    output: list[Any] = [None]
    active: set[int] = set()
    pending = [(False, value, output, 0)]
    while pending:
        leaving, item, parent, key = pending.pop()
        if leaving:
            active.remove(id(item))
            continue
        kind = type(item)
        if item is None or kind is bool:
            parent[key] = item
        elif kind is str:
            _utf8(item)
            parent[key] = item
        elif kind in (int, float):
            if not _number(item) or (int(item) == item and (not _safe_integer(item))):
                raise _invalid()
            parent[key] = item
        elif kind in (dict, list):
            if id(item) in active:
                raise _invalid()
            active.add(id(item))
            pending.append((True, item, None, None))
            if kind is dict:
                for name in item:
                    if type(name) is not str:
                        raise _invalid()
                    _utf8(name)
                copied: Any = dict.fromkeys(item)
                parent[key] = copied
                for name in reversed(item):
                    pending.append((False, item[name], copied, name))
            else:
                copied = [None] * len(item)
                parent[key] = copied
                for index in range(len(item) - 1, -1, -1):
                    pending.append((False, item[index], copied, index))
        else:
            raise _invalid()
    return output[0]

def _digest(value: Any) -> str:
    digest = hashlib.sha256()
    pending = [(False, value)]
    while pending:
        raw, item = pending.pop()
        if raw:
            digest.update(item)
        elif item is None:
            digest.update(b'n')
        elif type(item) is bool:
            digest.update(b't' if item else b'f')
        elif type(item) in (int, float):
            digest.update(b'd' + struct.pack('>d', 0.0 if item == 0 else float(item)).hex().encode('ascii'))
        elif type(item) is str:
            encoded = _utf8(item)
            digest.update(b's' + str(len(encoded)).encode('ascii') + b':' + encoded)
        elif type(item) is list:
            digest.update(b'[')
            pending.append((True, b']'))
            pending.extend(((False, child) for child in reversed(item)))
        else:
            digest.update(b'{')
            pending.append((True, b'}'))
            for name in reversed(sorted(item, key=lambda key: key.encode('utf-8'))):
                pending.append((False, item[name]))
                pending.append((False, name))
    return digest.hexdigest()

def flow_request_hash(request: Any) -> str:
    """Hash every exact JSON value, independent of object order/JSON whitespace."""
    return _digest(_snapshot(request))

def _run_id_valid(run_id: Any) -> bool:
    return type(run_id) is str and 1 <= len(_utf8(run_id)) <= 256

def flow_idempotency_key(run_id: str, step_id: str, request: Any) -> str:
    """Derive a content-bound key for one run and one declared step."""
    if not _run_id_valid(run_id) or type(step_id) is not str or (not _STEP_ID.fullmatch(step_id)):
        raise _invalid()
    return 'sz-flow-v1-' + _digest([run_id, step_id, flow_request_hash(request)])

def _object(value: Any, required: set[str], optional: Optional[set[str]]=None) -> None:
    if type(value) is not dict or not required.issubset(value) or (not set(value).issubset(required | (optional or set()))):
        raise _invalid()

def _reference(value: Mapping[str, Any], index: int, positions: Mapping[str, int], steps: list[Dict[str, Any]], *, current: bool=False) -> None:
    name, question = (value.get('step'), value.get('question'))
    if type(name) is not str or name not in positions or type(question) is not str or (positions[name] >= index + int(current)) or (question not in steps[positions[name]]['questions']):
        raise _invalid()

def _condition(value: Any, index: int, positions: Mapping[str, int], steps: list[Dict[str, Any]], *, current: bool=False) -> None:
    if type(value) is not dict:
        raise _invalid()
    operation = value.get('op')
    _object(value, {'step', 'question', 'op', 'values' if operation == 'in' else 'value'})
    _reference(value, index, positions, steps, current=current)
    if operation == 'eq':
        if type(value['value']) not in (bool, str):
            raise _invalid()
    elif operation == 'in':
        if type(value['values']) is not list or any((type(item) not in (bool, str) for item in value['values'])):
            raise _invalid()
    elif operation in ('confidence_gte', 'confidence_lt'):
        if not _number(value['value']) or not 0 <= value['value'] <= 1:
            raise _invalid()
    else:
        raise _invalid()

def _template(value: Any, index: int, positions: Mapping[str, int], steps: list[Dict[str, Any]]) -> None:
    pending = [value]
    while pending:
        item = pending.pop()
        if type(item) is not dict or len(item) != 1:
            raise _invalid()
        tag, content = next(iter(item.items()))
        if tag == 'literal':
            continue
        if tag == 'state':
            if content is not True:
                raise _invalid()
        elif tag == 'answer':
            _object(content, {'step', 'question', 'field'})
            _reference(content, index, positions, steps)
            if content['field'] not in ('decision', 'confidence', 'alternatives'):
                raise _invalid()
        elif tag == 'object':
            if type(content) is not dict:
                raise _invalid()
            pending.extend(content.values())
        elif tag == 'array':
            if type(content) is not list:
                raise _invalid()
            pending.extend(content)
        else:
            raise _invalid()

def validate_flow(definition: Mapping[str, Any]) -> FlowDefinition:
    """Validate all flow/control references before I/O and return a JSON snapshot.

    The ordinary decide endpoint owns question, attachment and context limits.
    Every edge points forward, so even unreachable cycles are rejected.
    """
    flow = _snapshot(definition)
    _object(flow, {'version', 'request', 'steps'})
    if type(flow['version']) not in (int, float) or flow['version'] != 1:
        raise _invalid()
    request, steps = (flow['request'], flow['steps'])
    _object(request, {'model', 'state'}, {'attachments', 'session_id', 'prompt_cache_key', 'route', 'mode'})
    if type(steps) is not list or not 1 <= len(steps) <= MAX_FLOW_STEPS:
        raise _invalid()
    positions: Dict[str, int] = {}
    for index, step in enumerate(steps):
        _object(step, {'id', 'questions', 'next'}, {'depends_on', 'state_template', 'attachments'})
        name = step['id']
        if type(name) is not str or not _STEP_ID.fullmatch(name) or name in positions:
            raise _invalid()
        if type(step['questions']) is not dict or not step['questions']:
            raise _invalid()
        positions[name] = index
    for index, step in enumerate(steps):
        dependencies = step.get('depends_on', [])
        if type(dependencies) is not list:
            raise _invalid()
        for condition in dependencies:
            _condition(condition, index, positions, steps)
        if 'state_template' in step:
            _template(step['state_template'], index, positions, steps)
        branch = step['next']
        _object(branch, {'rules', 'default'})
        if type(branch['rules']) is not list:
            raise _invalid()
        destinations = [branch['default']]
        for rule in branch['rules']:
            _object(rule, {'when', 'next'})
            _condition(rule['when'], index, positions, steps, current=True)
            destinations.append(rule['next'])
        for destination in destinations:
            if destination is not None and (type(destination) is not str or destination not in positions or positions[destination] <= index):
                raise _invalid()
    return cast(FlowDefinition, flow)

def _matches(condition: Mapping[str, Any], answers: Mapping[str, Mapping[str, Any]]) -> bool:
    answer = answers.get(condition['step'], {}).get(condition['question'])
    if answer is None or answer.kind == 'refusal':
        return False
    operation = condition['op']
    if operation == 'eq':
        return type(answer.decision) is type(condition['value']) and answer.decision == condition['value']
    if operation == 'in':
        return any((type(answer.decision) is type(value) and answer.decision == value for value in condition['values']))
    if answer.confidence is None:
        return False
    return answer.confidence >= condition['value'] if operation == 'confidence_gte' else answer.confidence < condition['value']

def _render(template: Mapping[str, Any], state: Any, answers: Mapping[str, Mapping[str, Any]]) -> Any:
    output: list[Any] = [None]
    pending = [(template, output, 0)]
    while pending:
        item, parent, key = pending.pop()
        tag, content = next(iter(item.items()))
        if tag == 'literal':
            parent[key] = _snapshot(content)
        elif tag == 'state':
            parent[key] = _snapshot(state)
        elif tag == 'answer':
            answer = answers.get(content['step'], {}).get(content['question'])
            if answer is None or answer.kind == 'refusal':
                raise _invalid()
            parent[key] = _snapshot(getattr(answer, content['field']))
        elif tag == 'object':
            copied: Any = dict.fromkeys(content)
            parent[key] = copied
            pending.extend(((child, copied, name) for name, child in reversed(list(content.items()))))
        else:
            copied = [None] * len(content)
            parent[key] = copied
            pending.extend(((content[index], copied, index) for index in range(len(content) - 1, -1, -1)))
    return output[0]

def _question_refusal(code: str, request_id: Optional[str]) -> SystemZeroError:
    held = code in _HELD_CODES
    message = _HELD_MESSAGES.get(code) if held else _QUESTION_REFUSALS.get(code)
    if message is None:
        return _unavailable(request_id)
    kind = 'not_found_error' if held else 'invalid_request_error'
    return error_class(kind, code)(message, status=404 if held else 422, type=kind, code=code, request_id=request_id, retryable=False)

def _normalize_error(error: Exception, request_id: Optional[str]=None) -> SystemZeroError:
    if type(error) is APIConnectionError:
        return error
    if isinstance(error, APITimeoutError) and error.code is None:
        return _timeout(error.request_id or request_id)
    if isinstance(error, SystemZeroError) and error.code is not None:
        return error
    return _unavailable(error.request_id or request_id if isinstance(error, SystemZeroError) else request_id)

def _safe_error(error: SystemZeroError) -> Dict[str, Any]:
    return {'code': error.code if error.code in _KNOWN_CODES else 'service_unavailable', 'type': error.type if error.type in _ERROR_TYPES else 'api_error', 'status': error.status if _safe_integer(error.status) and 100 <= error.status <= 599 else None, 'retryable': error.retryable is True}

def _usage(body: Any) -> Optional[Dict[str, Any]]:
    if type(body) is not dict:
        return None
    usage = body.get('usage')
    if type(usage) is not dict:
        return None
    return {name: usage[name] for name in _USAGE_FIELDS if name in usage and _number(usage[name]) and (usage[name] >= 0)}

def _client_call_bound(client: SystemZero) -> float:
    timeout, wait, retries, budget = (client.timeout, client.max_retry_wait, client.max_retries, client.retry_budget)
    if not _number(timeout) or timeout <= 0 or (not _number(wait)) or (wait < 0) or (not _safe_integer(retries)) or (retries < 0) or (not _number(budget)) or (budget < 0):
        raise _invalid()
    retries = min(retries, 5) if budget > 0 else 0
    bound = min(timeout * (retries + 1) + wait * retries, max(timeout, budget) if retries else timeout) * 1000
    if not _number(bound) or bound < 1:
        raise _invalid()
    return bound

def run_flow(client: SystemZero, definition: Mapping[str, Any], *, run_id: str, deadline_ms: float, include_content: bool=False, max_decide_calls: Optional[int]=None, now: Optional[Callable[[], float]]=None) -> FlowTrace:
    """Run ordered, bounded decisions and return a content-free trace by default.

    Errors retain their SDK class and carry the partial trace on ``error.trace``.
    The deadline admits a call only when its full SDK retry bound fits. Injected
    transports must obey the client's timeout contract. Client settings are never
    modified, and the function does not create workers or background operations.
    """
    clock = now if now is not None else lambda: time.monotonic() * 1000
    if not callable(clock):
        raise _invalid()
    try:
        started = clock()
    except Exception:
        raise _invalid() from None
    if not _number(started) or started < 0:
        raise _invalid()
    flow = validate_flow(definition)
    trace: Dict[str, Any] = {'version': 1, 'run_hash': '', 'status': 'failed', 'started_ms': started, 'elapsed_ms': 0, 'decide_calls': 0, 'steps': []}
    max_calls = MAX_FLOW_STEPS if max_decide_calls is None else max_decide_calls
    active_step: Optional[Dict[str, Any]] = None
    origin = 'local'
    last_now = started

    def timestamp() -> float:
        nonlocal last_now
        try:
            value = clock()
        except Exception:
            raise _invalid() from None
        if not _number(value) or value < last_now:
            raise _invalid()
        last_now = value
        return value

    def check_deadline(call_bound: float=0) -> None:
        remaining = deadline_ms - (timestamp() - started)
        if remaining <= 0 or remaining < call_bound:
            raise _timeout()
    try:
        if not _safe_integer(max_calls) or not 0 <= max_calls <= MAX_FLOW_STEPS:
            raise InvalidRequestError('max_decide_calls must be a nonnegative safe integer at most 32.', status=400, type='invalid_request_error', code='invalid_request')
        if not _run_id_valid(run_id) or not _safe_integer(deadline_ms) or (not 1 <= deadline_ms <= 2147483647) or (type(include_content) is not bool):
            raise _invalid()
        trace['run_hash'] = _digest(run_id)
        _client_call_bound(client)
        steps = flow['steps']
        positions = {step['id']: index for index, step in enumerate(steps)}
        state = flow['request']['state']
        answers: Dict[str, Dict[str, Any]] = {}
        index: Optional[int] = 0
        while index is not None:
            step = steps[index]
            active_step = {'step_index': index, 'status': 'failed', 'request_id': None, 'request_hash': None, 'idempotency_key': None, 'question_count': len(step['questions']), 'started_ms': timestamp(), 'elapsed_ms': 0, 'decide_calls': 0, 'replayed': False, 'usage': None, 'answers': [], 'branch': None}
            if include_content:
                active_step.update(step_id=step['id'], questions=_snapshot(step['questions']))
            trace['steps'].append(active_step)
            if len(trace['steps']) > MAX_FLOW_STEPS:
                raise _invalid()
            check_deadline()
            branch = step['next']
            if not all((_matches(condition, answers) for condition in step.get('depends_on', []))):
                destination = branch['default']
                active_step['status'] = 'skipped'
                active_step['branch'] = {'rule_index': None, 'next_step_index': positions[destination] if destination is not None else None, 'reason': 'dependency_unmet'}
            else:
                if 'state_template' in step:
                    state = _render(step['state_template'], state, answers)
                request = {**flow['request'], 'state': state, 'questions': step['questions']}
                if 'attachments' in step:
                    request['attachments'] = step['attachments']
                request_hash = _digest(request)
                key = 'sz-flow-v1-' + _digest([run_id, step['id'], request_hash])
                active_step.update(request_hash=request_hash, idempotency_key=key)
                if include_content:
                    active_step['request'] = _snapshot(request)
                check_deadline(_client_call_bound(client))
                if trace['decide_calls'] >= max_calls:
                    raise SystemZeroError('The local call limit has been reached.', code='call_limit_reached')
                trace['decide_calls'] += 1
                active_step['decide_calls'] = 1
                origin = 'http'
                token = _retry_context.set({'workflow_recovery': True})
                try:
                    response = client.decide_with_response(request, idempotency_key=key)
                finally:
                    _retry_context.reset(token)
                origin = 'local'
                body = response.data
                active_step['request_id'] = response.metadata.request_id
                active_step['replayed'] = response.metadata.replayed is True
                active_step['usage'] = _usage(body)
                if type(body) is not dict or type(body.get('answers')) is not dict or set(body['answers']) != set(step['questions']):
                    raise _unavailable(active_step['request_id'])
                try:
                    normalized = decisions_for(body, request)
                except (ValueError, TypeError, OverflowError, KeyError):
                    raise _unavailable(active_step['request_id']) from None
                refusal = None
                for question_index, question in enumerate(sorted(step['questions'], key=lambda name: name.encode('utf-8'))):
                    answer = normalized[question]
                    entry = {'question_index': question_index, 'kind': answer.kind, 'confidence': answer.confidence}
                    if include_content:
                        entry['question'] = question
                        if answer.kind != 'refusal':
                            entry.update(decision=answer.decision, alternatives=_snapshot(list(answer.alternatives)))
                    active_step['answers'].append(entry)
                    if answer.kind == 'refusal' and refusal is None:
                        refusal = _question_refusal(answer.refusal.code, active_step['request_id'])
                if refusal is not None:
                    origin = 'refusal' if refusal.code != 'service_unavailable' else 'local'
                    raise refusal
                answers[step['id']] = normalized
                selected = None
                destination = branch['default']
                for rule_index, rule in enumerate(branch['rules']):
                    if _matches(rule['when'], answers):
                        selected, destination = (rule_index, rule['next'])
                        break
                active_step['status'] = 'completed'
                active_step['branch'] = {'rule_index': selected, 'next_step_index': positions[destination] if destination is not None else None, 'reason': 'rule_matched' if selected is not None else 'default'}
            check_deadline()
            active_step['elapsed_ms'] = timestamp() - active_step['started_ms']
            index = active_step['branch']['next_step_index']
        trace['status'] = 'completed'
        trace['elapsed_ms'] = timestamp() - started
        if trace['elapsed_ms'] >= deadline_ms:
            raise _timeout(active_step['request_id'] if active_step else None)
        return cast(FlowTrace, trace)
    except Exception as error:
        typed = _normalize_error(error, active_step['request_id'] if active_step else None)
        try:
            timestamp()
        except Exception:
            pass
        status = 'failed'
        if origin in ('http', 'refusal'):
            if typed.code in _HELD_CODES:
                status = 'held'
            elif typed.status is not None and 400 <= typed.status < 500:
                status = 'refused'
        trace['status'] = status
        trace['elapsed_ms'] = max(0, last_now - started)
        if active_step is not None:
            active_step['status'] = status
            active_step['elapsed_ms'] = max(0, last_now - active_step['started_ms'])
            active_step['error'] = _safe_error(typed)
            if typed.request_id is not None:
                active_step['request_id'] = typed.request_id
        typed.trace = trace
        raise typed from None
