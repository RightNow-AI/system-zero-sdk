"""Content-free evaluation of caller-labelled decisions; no network or billing side effects."""
import hashlib
import json
import math
import re
from typing import Any, Dict, Mapping, Optional, Sequence
from .decisions import decisions_for
from .error_codes import ErrorCode
from .errors import APIError, InvalidRequestError
from .workflow_types import EvaluationReport, QuestionScore
_TYPES = ('boolean', 'choice', 'score')
_USAGE = ('input_tokens', 'cached_input_tokens', 'media_tokens', 'long_input_tokens', 'media_items', 'thought_tokens')
_KNOWN_CODES = frozenset((code.value for code in ErrorCode))
_QUESTION_REFUSAL_CODES = frozenset(('injection_suspected', 'question_not_grounded'))
_SEMANTIC_REFUSAL_CODES = _QUESTION_REFUSAL_CODES | {'request_refused'}
_MAX_INTEGER = 9007199254740991

def _input_error():
    raise InvalidRequestError('Invalid batch evaluation input.', code='invalid_request', status=400, type='invalid_request_error')

def _reply_error():
    raise APIError('The decision reply cannot be evaluated.', code='service_unavailable', status=503, type='api_error') from None

def _nonnegative(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False

def _integer(value: Any) -> bool:
    return _nonnegative(value) and value <= _MAX_INTEGER and (int(value) == value)

def _add(first: int, second: int) -> int:
    result = first + second
    if not _integer(result):
        _input_error()
    return int(result)

def _utf8(value: str) -> bytes:
    return value.encode('utf-16-le', 'surrogatepass').decode('utf-16-le', 'replace').encode('utf-8')

def _ascii_json(value: Any, pretty: bool=False) -> str:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2 if pretty else None, separators=None if pretty else (',', ':'))

def _choices(question: Mapping[str, Any]):
    return sorted(question['criteria'], key=_utf8) if question['type'] == 'choice' else []

def _choice_schema(labels: Sequence[str]) -> str:
    return hashlib.sha256(_ascii_json(labels).encode('ascii')).hexdigest()

def validate_expected(request: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    """Check labels only; decide owns question, context, attachment and media limits."""
    if not isinstance(request, Mapping) or not isinstance(request.get('questions'), Mapping) or (not isinstance(expected, Mapping)):
        _input_error()
    questions = request['questions']
    if set(questions) != set(expected):
        _input_error()
    for name, question in questions.items():
        if not isinstance(name, str) or not isinstance(question, Mapping) or question.get('type') not in _TYPES:
            _input_error()
        value = expected[name]
        if value is None:
            continue
        if question['type'] == 'boolean':
            if type(value) is not bool:
                _input_error()
        elif question['type'] == 'choice':
            if not isinstance(question.get('criteria'), Mapping) or not isinstance(value, str) or value not in question['criteria']:
                _input_error()
        elif not isinstance(question.get('criteria'), list) or not isinstance(value, str) or re.fullmatch('(0|[1-9][0-9]*)', value) is None or (len(value) > 16) or (int(value) > _MAX_INTEGER) or (int(value) >= len(question['criteria'])):
            _input_error()

def _refusal_score(question_id, question, wanted, code):
    labels = _choices(question)
    return {'question_id': question_id, 'type': question['type'], 'outcome': 'refused', 'expected_refusal': wanted is None, 'correct': wanted is None, 'confidence': None, 'choice_expected': labels.index(wanted) if question['type'] == 'choice' and isinstance(wanted, str) else None, 'choice_actual': None, 'choice_schema': _choice_schema(labels) if question['type'] == 'choice' else None, 'code': code}

def score_refusal(request: Mapping[str, Any], expected: Mapping[str, Any], code: str) -> list[QuestionScore]:
    """Score a typed whole-request semantic refusal, excluding held and operational codes."""
    validate_expected(request, expected)
    if not isinstance(code, str) or code not in _SEMANTIC_REFUSAL_CODES:
        _reply_error()
    try:
        return [_refusal_score(name, request['questions'][name], expected[name], code) for name in sorted(request['questions'], key=_utf8)]
    except Exception:
        _reply_error()

def score_reply(request: Mapping[str, Any], expected: Mapping[str, Any], reply: Mapping[str, Any]) -> list[QuestionScore]:
    """Discard text, labels and raw answers after projecting scoring facts."""
    validate_expected(request, expected)
    try:
        questions = request['questions']
        if not isinstance(reply, Mapping) or not isinstance(reply.get('answers'), Mapping) or set(reply['answers']) != set(questions):
            _reply_error()
        results = decisions_for(reply, request)
        rows = []
        for question_id in sorted(questions, key=_utf8):
            question, result, wanted = (questions[question_id], results[question_id], expected[question_id])
            labels = _choices(question)
            schema = _choice_schema(labels) if question['type'] == 'choice' else None
            if result.kind == 'refusal':
                code = result.refusal.code
                if code not in _QUESTION_REFUSAL_CODES:
                    _reply_error()
                rows.append(_refusal_score(question_id, question, wanted, code))
                continue
            raw = reply['answers'][question_id]
            if raw['type'] != question['type']:
                _reply_error()
            if question['type'] != 'boolean':
                keys = labels if question['type'] == 'choice' else [str(index) for index in range(len(question['criteria']))]
                if set(raw['probabilities']) != set(keys):
                    _reply_error()
            correct = type(result.decision) is type(wanted) and result.decision == wanted
            actual = labels.index(result.decision) if question['type'] == 'choice' else None
            rows.append({'question_id': question_id, 'type': question['type'], 'outcome': 'correct' if correct else 'wrong', 'expected_refusal': wanted is None, 'correct': correct, 'confidence': result.confidence, 'choice_expected': labels.index(wanted) if question['type'] == 'choice' and isinstance(wanted, str) else None, 'choice_actual': actual, 'choice_schema': schema, 'code': None})
        return rows
    except Exception:
        _reply_error()

def _project_question(value: Mapping[str, Any]):
    if not isinstance(value, Mapping) or not isinstance(value.get('question_id'), str) or value.get('type') not in _TYPES or (value.get('outcome') not in ('correct', 'wrong', 'refused')) or (type(value.get('correct')) is not bool) or (type(value.get('expected_refusal')) is not bool):
        _input_error()
    confidence = value.get('confidence')
    code = value.get('code')
    if 'confidence' not in value or 'code' not in value or (confidence is not None and (not _nonnegative(confidence) or confidence > 1 or confidence != math.floor(confidence * 1000000 + 0.5) / 1000000)) or (code is not None and (not isinstance(code, str) or code not in _KNOWN_CODES)):
        _input_error()
    refused = value['outcome'] == 'refused'
    if value['correct'] != (value['expected_refusal'] if refused else value['outcome'] == 'correct') or (refused and (confidence is not None or code not in _SEMANTIC_REFUSAL_CODES)) or (not refused and code is not None) or (value['expected_refusal'] and (not refused) and value['correct']):
        _input_error()
    for field in ('choice_expected', 'choice_actual'):
        if field not in value or (value[field] is not None and (not _integer(value[field]))):
            _input_error()
    if value['type'] == 'choice':
        if not isinstance(value.get('choice_schema'), str) or re.fullmatch('[a-f0-9]{64}', value['choice_schema']) is None or value['expected_refusal'] != (value['choice_expected'] is None) or (refused != (value['choice_actual'] is None)):
            _input_error()
    elif value['choice_expected'] is not None or value['choice_actual'] is not None or 'choice_schema' not in value or (value['choice_schema'] is not None):
        _input_error()
    return {key: value[key] for key in ('question_id', 'type', 'outcome', 'expected_refusal', 'correct', 'confidence', 'choice_expected', 'choice_actual', 'choice_schema', 'code')}

def _project_record(value: Mapping[str, Any]):
    if not isinstance(value, Mapping) or not isinstance(value.get('id'), str) or value.get('outcome') not in ('completed', 'refused', 'held', 'error') or (not _nonnegative(value.get('latency_ms'))) or (not isinstance(value.get('questions'), list)):
        _input_error()
    code = value.get('code')
    if 'code' not in value or (code is not None and (not isinstance(code, str) or code not in _KNOWN_CODES)):
        _input_error()
    usage = None
    if 'usage' not in value:
        _input_error()
    if value['usage'] is not None:
        if not isinstance(value['usage'], Mapping) or any((not _integer(value['usage'].get(key)) for key in _USAGE)):
            _input_error()
        usage = {key: int(value['usage'][key]) for key in _USAGE}
    questions = [] if value['outcome'] in ('held', 'error') else [_project_question(row) for row in value['questions']]
    if len({row['question_id'] for row in questions}) != len(questions):
        _input_error()
    return {'id': value['id'], 'outcome': value['outcome'], 'questions': questions, 'latency_ms': value['latency_ms'], 'usage': usage, 'code': code}

def _accuracy(rows: Sequence[Mapping[str, Any]]):
    correct = sum((row['correct'] for row in rows))
    expected_answers = sum((not row['expected_refusal'] for row in rows))
    false_refusals = sum((not row['expected_refusal'] and row['outcome'] == 'refused' for row in rows))
    return {'questions': len(rows), 'correct': correct, 'wrong': sum((row['outcome'] == 'wrong' for row in rows)), 'refused': sum((row['outcome'] == 'refused' for row in rows)), 'expected_answers': expected_answers, 'false_refusals': false_refusals, 'accuracy': correct / len(rows) if rows else None, 'false_refusal_rate': false_refusals / expected_answers if expected_answers else None}

def _percentile(ordered: Sequence[float], p: float) -> Optional[float]:
    if not ordered:
        return None
    index = (len(ordered) - 1) * p
    lower = math.floor(index)
    return ordered[lower] + (ordered[math.ceil(index)] - ordered[lower]) * (index - lower)

def evaluate(records: Sequence[Mapping[str, Any]], *, bins: int=10) -> EvaluationReport:
    """Use quality attempts only; report selected-outcome calibration on caller data."""
    if not isinstance(records, (list, tuple)) or not _integer(bins) or (not 1 <= bins <= 100):
        _input_error()
    bins = int(bins)
    projected = [_project_record(row) for row in records]
    if len({row['id'] for row in projected}) != len(projected):
        _input_error()
    quality = [row for row in projected if row['outcome'] in ('completed', 'refused')]
    questions = [question for row in quality for question in row['questions']]
    latency = sorted((row['latency_ms'] for row in quality))
    totals = {key: 0 for key in _USAGE}
    with_usage = 0
    for row in projected:
        if row['usage'] is not None:
            with_usage += 1
            for key in _USAGE:
                totals[key] = _add(totals[key], row['usage'][key])
    confusion: Dict[Any, Any] = {}
    for row in questions:
        if row['type'] != 'choice' or row['choice_expected'] is None or row['choice_actual'] is None:
            continue
        key = (row['question_id'], row['choice_schema'])
        group = confusion.setdefault(key, {'question_id': row['question_id'], 'choice_schema': row['choice_schema'], 'counts': []})
        cell = next((cell for cell in group['counts'] if cell['expected'] == row['choice_expected'] and cell['actual'] == row['choice_actual']), None)
        if cell is None:
            group['counts'].append({'expected': row['choice_expected'], 'actual': row['choice_actual'], 'count': 1})
        else:
            cell['count'] += 1
    choice_confusion = sorted(confusion.values(), key=lambda row: (_utf8(row['question_id']), row['choice_schema']))
    for group in choice_confusion:
        group['counts'].sort(key=lambda cell: (cell['expected'], cell['actual']))
    confidence_rows = [row for row in questions if row['confidence'] is not None]
    calibration = None
    if confidence_rows:
        reliability = [{'lower': index / bins, 'upper': (index + 1) / bins, 'count': 0, 'accuracy': None, 'mean_confidence': None} for index in range(bins)]
        buckets = [{'correct': 0, 'confidence': 0.0} for _ in range(bins)]
        squared_error = 0.0
        for row in confidence_rows:
            confidence = row['confidence']
            index = min(bins - 1, math.floor(math.floor(confidence * 1000000 + 0.5) * bins / 1000000))
            reliability[index]['count'] += 1
            buckets[index]['correct'] += int(row['correct'])
            buckets[index]['confidence'] += confidence
            squared_error += (confidence - int(row['correct'])) ** 2
        ece = 0.0
        for index, bucket in enumerate(reliability):
            if not bucket['count']:
                continue
            bucket['accuracy'] = buckets[index]['correct'] / bucket['count']
            bucket['mean_confidence'] = buckets[index]['confidence'] / bucket['count']
            ece += bucket['count'] / len(confidence_rows) * abs(bucket['accuracy'] - bucket['mean_confidence'])
        calibration = {'scope': 'caller_data_only', 'label': "Computed on the caller's data only.", 'method': 'selected_outcome_correctness', 'count': len(confidence_rows), 'bins': reliability, 'ece': ece, 'brier_score': squared_error / len(confidence_rows)}
    return {'version': 1, 'items': {'total': len(projected), **{outcome: sum((row['outcome'] == outcome for row in projected)) for outcome in ('completed', 'refused', 'held', 'error')}}, 'metrics': _accuracy(questions), 'by_type': {kind: _accuracy([row for row in questions if row['type'] == kind]) for kind in _TYPES}, 'choice_confusion': choice_confusion, 'latency_ms': {'count': len(latency), 'p50': _percentile(latency, 0.5), 'p95': _percentile(latency, 0.95)}, 'usage': {'totals': totals, 'records_with_usage': with_usage, 'records_without_usage': len(projected) - with_usage}, 'calibration': calibration, 'records': projected}

def report_json(report: Mapping[str, Any]) -> str:
    return _ascii_json(report, pretty=True) + '\n'

def _markdown(value: Any) -> str:
    if value is None:
        return 'unavailable'
    return re.sub('[&<>|\\\\`*\\[\\]\\r\\n\\u0080-\\uffff]', lambda match: '&#' + str(ord(match.group(0))) + ';', str(value))

def report_markdown(report: Mapping[str, Any]) -> str:
    items, metrics, latency, usage = (report[key] for key in ('items', 'metrics', 'latency_ms', 'usage'))
    lines = ['# Batch evaluation ', '', '| Items | Completed | Refused | Held | Errors |', '| --- | --- | --- | --- | --- |', f"| {items['total']} | {items['completed']} | {items['refused']} | {items['held']} | {items['error']} |", '', '| Type | Questions | Correct | Accuracy | False-refusal rate |', '| --- | --- | --- | --- | --- |']
    for kind in _TYPES:
        row = report['by_type'][kind]
        lines.append(f"| {kind} | {row['questions']} | {row['correct']} | {_markdown(row['accuracy'])} | {_markdown(row['false_refusal_rate'])} |")
    lines.extend(['', f"Accuracy: {_markdown(metrics['accuracy'])}; false-refusal rate: {_markdown(metrics['false_refusal_rate'])}.", '', f"Latency ms (linear interpolation): p50 {_markdown(latency['p50'])}, p95 {_markdown(latency['p95'])}; samples {latency['count']}.", '', f"Usage supplied by {usage['records_with_usage']} items; unavailable for {usage['records_without_usage']} items.", '', '| Usage field | Total |', '| --- | --- |'])
    lines.extend((f"| {key} | {usage['totals'][key]} |" for key in _USAGE))
    lines.append('')
    calibration = report['calibration']
    if calibration is not None:
        lines.extend([calibration['label'], '', f"Selected-outcome correctness: ECE {calibration['ece']}; Brier score {calibration['brier_score']}; samples {calibration['count']}.", '', '| Lower | Upper | Count | Accuracy | Mean confidence |', '| --- | --- | --- | --- | --- |'])
        lines.extend((f"| {bucket['lower']} | {bucket['upper']} | {bucket['count']} | {_markdown(bucket['accuracy'])} | {_markdown(bucket['mean_confidence'])} |" for bucket in calibration['bins']))
        lines.append('')
    lines.extend(['| Question ID | Choice schema | Expected index | Actual index | Count |', '| --- | --- | --- | --- | --- |'])
    for group in report['choice_confusion']:
        for cell in group['counts']:
            lines.append(f"| {_markdown(group['question_id'])} | {group['choice_schema']} | {cell['expected']} | {cell['actual']} | {cell['count']} |")
    lines.extend(['', '| Item ID | Outcome | Code | Latency ms |', '| --- | --- | --- | --- |'])
    for row in report['records']:
        lines.append(f"| {_markdown(row['id'])} | {row['outcome']} | {_markdown(row['code'])} | {row['latency_ms']} |")
    return '\n'.join(lines) + '\n'
