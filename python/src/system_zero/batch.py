"""Bounded ordinary decide calls with durable, content-free batch receipts."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
import time
from typing import Any, Callable, Mapping, Optional, Sequence
from .client import SystemZero, _checked_base_url
from ._retry import _RetryContext, _retry_context
from .errors import APIError, InvalidRequestError, SystemZeroError
from .public_error_codes import KNOWN_ERROR_CODES
from .evaluate import evaluate as _evaluate, score_refusal, score_reply, validate_expected
from .workflow_types import BatchResult
MAX_SAFE_INTEGER = 9007199254740991
REPLAY_TTL_MS = 86400000
_SAFE_ID = re.compile('[A-Za-z0-9_.-]{1,128}\\Z', re.ASCII)
_USAGE_KEYS = ('input_tokens', 'cached_input_tokens', 'media_tokens', 'long_input_tokens', 'media_items', 'thought_tokens')
_HELD = frozenset('long_context_not_available long_recording_not_available video_not_available image_not_available audio_not_available document_not_available committee_not_available deliberate_not_available'.split())
_REFUSALS = frozenset('question_not_grounded injection_suspected request_refused'.split())
_KNOWN_CODES = KNOWN_ERROR_CODES

def _invalid(message: str='The batch configuration or receipt is invalid.', *, param: Optional[str]=None) -> InvalidRequestError:
    return InvalidRequestError(message, status=400, type='invalid_request_error', code='invalid_request', param=param)

def _integer(value: Any) -> bool:
    return type(value) in (int, float) and 0 <= value <= MAX_SAFE_INTEGER and (value == int(value))

def _ordered_keys(value: Mapping[str, Any]) -> list[str]:
    indices = [key for key in value if re.fullmatch('0|[1-9][0-9]{0,9}', key, re.ASCII) and int(key) < 4294967295]
    indexed = set(indices)
    return sorted(indices, key=int) + [key for key in value if key not in indexed]

def _snapshot_order(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _snapshot_order(value[key]) for key in _ordered_keys(value)}
    if isinstance(value, list):
        return [_snapshot_order(part) for part in value]
    return value

def _canonical(value: Any) -> bytes:
    if value is None:
        return b'n'
    if type(value) is bool:
        return b't' if value else b'f'
    if type(value) in (int, float):
        number = float(value)
        if not math.isfinite(number) or (number.is_integer() and abs(number) > MAX_SAFE_INTEGER):
            raise _invalid('Batch JSON numbers must be finite and integral values must be safe integers.')
        return b'd' + struct.pack('>d', 0.0 if number == 0 else number).hex().encode('ascii')
    if isinstance(value, str):
        encoded = value.encode('utf-8')
        return b's' + str(len(encoded)).encode('ascii') + b':' + encoded
    if isinstance(value, list):
        return b'a' + str(len(value)).encode('ascii') + b':' + b''.join((_canonical(part) for part in value))
    if isinstance(value, dict) and all((isinstance(key, str) for key in value)):
        return b'o' + str(len(value)).encode('ascii') + b':' + b''.join((_canonical(key) + _canonical(value[key]) for key in _ordered_keys(value)))
    raise _invalid('Batch requests and expected answers must contain JSON values only.')

def request_fingerprint(value: Any) -> str:
    """Hash the complete ordered JSON value with cross-SDK numeric encoding."""
    try:
        return hashlib.sha256(_canonical(value)).hexdigest()
    except (OverflowError, UnicodeError, RecursionError) as error:
        raise _invalid() from error

def idempotency_key(run_id: str, item_id: str, request_hash: str) -> str:
    if not isinstance(run_id, str) or not _SAFE_ID.fullmatch(run_id) or (not isinstance(item_id, str)) or (not _SAFE_ID.fullmatch(item_id)):
        raise _invalid('Batch run and item ids must be ASCII identifiers of 1 to 128 characters.')
    if not isinstance(request_hash, str) or not re.fullmatch('[0-9a-f]{64}', request_hash):
        raise _invalid()
    return 'szbatch_' + hashlib.sha256((run_id + '\x00' + item_id + '\x00' + request_hash).encode('ascii')).hexdigest()

def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')

def _reported_usage(reply: Any) -> Optional[dict[str, int]]:
    raw = reply.get('usage') if isinstance(reply, dict) else None
    if not isinstance(raw, dict) or any((not _integer(raw.get(key)) for key in _USAGE_KEYS)):
        return None
    usage = {key: int(raw[key]) for key in _USAGE_KEYS}
    return usage

def _append(path: Path, row: dict[str, Any]) -> None:
    payload = json.dumps(row, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii') + b'\n'
    with path.open('ab') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())

def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = path.read_bytes()
    if payload and (not payload.endswith(b'\n')):
        end = payload.rfind(b'\n') + 1
        with path.open('r+b') as stream:
            stream.truncate(end)
            stream.flush()
            os.fsync(stream.fileno())
        payload = payload[:end]
    rows = []
    for line in payload.splitlines():
        row = json.loads(line)
        if not isinstance(row, dict):
            raise _invalid()
        rows.append(row)
    return rows

def run_batch(client: SystemZero, items: Sequence[Mapping[str, Any]], *, run_id: str, results_path: str | Path, deadline_ms: float, concurrency: int=2, max_retries: Optional[int]=None, max_decide_calls: Optional[int]=None, now_ms: Callable[[], float]=lambda: time.time() * 1000, sleep_ms: Optional[Callable[[float], None]]=None, random: Optional[Callable[[], float]]=None) -> BatchResult:
    """Run or resume a batch, returning records and a nullable stopped reason.

    Each item uses the client's bounded HTTP loop with before-work recovery. max_retries
    can only lower its retry count. The initial timeout must fit before admission;
    recovery is additionally bounded by the batch deadline. In-flight calls drain.
    Keep the same API key, target and receipt files for replay. Unfinished
    admissions cannot replay after the server's 24-hour idempotency lifetime.
    """
    started = time.monotonic() * 1000
    max_calls = 1000 if max_decide_calls is None else max_decide_calls
    if not _integer(max_calls):
        raise _invalid(param='max_decide_calls')
    if type(concurrency) is not int or not 1 <= concurrency <= 2 or (max_retries is not None and (type(max_retries) is not int or not 0 <= max_retries <= MAX_SAFE_INTEGER)) or (type(deadline_ms) not in (int, float)) or (not math.isfinite(deadline_ms)) or (not 0 < deadline_ms <= 2147483647):
        raise _invalid('Use concurrency 1 or 2, a finite positive deadline, and a nonnegative retry cap.')
    timeout_ms = client.timeout * 1000
    if not math.isfinite(timeout_ms) or timeout_ms <= 0:
        raise _invalid()
    try:
        target = _checked_base_url(client.base_url)
    except ValueError as error:
        raise _invalid('The batch target URL is invalid.') from error
    target = hashlib.sha256(target.encode('utf-8')).hexdigest()
    wall_started = now_ms()
    if not math.isfinite(wall_started) or wall_started < 0 or wall_started > MAX_SAFE_INTEGER - deadline_ms:
        raise _invalid()
    deadline = started + deadline_ms
    prepared: dict[str, dict[str, Any]] = {}
    try:
        for raw in items:
            if not isinstance(raw, Mapping):
                raise _invalid()
            identifier = raw.get('id')
            request = raw.get('request')
            expected = raw.get('expected')
            request_fingerprint(request)
            request_fingerprint(expected)
            request = _snapshot_order(json.loads(_json_bytes(request)))
            expected = _snapshot_order(json.loads(_json_bytes(expected)))
            if not isinstance(request, dict) or not isinstance(expected, dict):
                raise _invalid()
            validate_expected(request, expected)
            digest = request_fingerprint(request)
            key = idempotency_key(run_id, identifier, digest)
            if identifier in prepared:
                raise _invalid('Batch item ids must be unique.')
            prepared[identifier] = {'request': request, 'expected': expected, 'admission': {'schema': 1, 'run_id': run_id, 'id': identifier, 'request_hash': digest, 'expected_hash': request_fingerprint(expected), 'idempotency_key': key, 'target': target}}
        idempotency_key(run_id, 'validation', '0' * 64)
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise _invalid() from error
    path = Path(results_path)
    journal = Path(str(path) + '.admissions.jsonl')
    lock = Path(str(path) + '.lock')
    owned = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with lock.open('xb') as stream:
            owned = True
            stream.write(run_id.encode('ascii'))
            stream.flush()
            os.fsync(stream.fileno())
        admissions: dict[str, dict[str, Any]] = {}
        finished: dict[str, dict[str, Any]] = {}
        journal_rows, result_rows = (_read(journal), _read(path))
        if any(('reserved_nanocents' in row or 'rate_card' in row for row in journal_rows + result_rows)):
            raise _invalid('Legacy cost-based batch receipts cannot be resumed; use a new results path.', param='results_path')
        for row in journal_rows:
            identifier = row.get('id')
            planned = prepared.get(identifier)
            if planned is None or identifier in admissions or (not _integer(row.get('admitted_at_ms'))):
                raise _invalid()
            header = {key: value for key, value in row.items() if key != 'admitted_at_ms'}
            if header != planned['admission']:
                raise _invalid('Resume requires the original run, target, items and expected answers.')
            admissions[identifier] = row
        for row in result_rows:
            identifier = row.get('id')
            admission = admissions.get(identifier)
            if admission is None or identifier in finished or any((row.get(key) != value for key, value in admission.items())):
                raise _invalid()
            allowed = set(admission) | {'outcome', 'code', 'attempts', 'latency_ms', 'usage', 'questions'}
            if set(row) != allowed:
                raise _invalid()
            if row.get('outcome') not in ('completed', 'held', 'error', 'refused') or (row.get('code') is not None and row['code'] not in _KNOWN_CODES) or (not _integer(row.get('attempts'))) or (type(row.get('latency_ms')) not in (int, float)) or (not math.isfinite(row['latency_ms'])) or (row['latency_ms'] < 0) or (not isinstance(row.get('questions'), list)):
                raise _invalid()
            if row['outcome'] == 'held' and row.get('code') not in _HELD:
                raise _invalid()
            if row['outcome'] != 'completed' and row['usage'] is not None:
                raise _invalid()
            if row['outcome'] == 'completed' and (row['code'] is not None or (row['usage'] is not None and _reported_usage({'usage': row['usage']}) != row['usage'])):
                raise _invalid('The saved usage is invalid.')
            if row['outcome'] in ('error', 'held'):
                if row['questions']:
                    raise _invalid()
            elif {score.get('question_id') for score in row['questions'] if isinstance(score, dict)} != set(prepared[identifier]['request']['questions']):
                raise _invalid()
            _evaluate([row])
            finished[identifier] = row
        if len(admissions) > max_calls:
            raise _invalid('The local call limit is below the existing admissions.', param='max_decide_calls')
        for identifier, admission in admissions.items():
            if identifier not in finished and (not admission['admitted_at_ms'] <= now_ms() < admission['admitted_at_ms'] + REPLAY_TTL_MS - timeout_ms):
                raise _invalid('An unfinished admission is outside its safe idempotency replay lifetime.')
        pending = [identifier for identifier in prepared if identifier not in finished]
        stopped: Optional[str] = None

        def execute(identifier: str) -> tuple[dict[str, Any], bool]:
            plan = prepared[identifier]
            admission = admissions[identifier]
            begin = time.monotonic() * 1000
            deadline_stop = False
            attempts = 0
            response: Any = None
            failure: Optional[SystemZeroError] = None

            def on_attempt() -> None:
                nonlocal attempts
                attempts += 1

            def on_deadline() -> None:
                nonlocal deadline_stop
                deadline_stop = True

            def remaining() -> float:
                return min(deadline - time.monotonic() * 1000, admission['admitted_at_ms'] + REPLAY_TTL_MS - now_ms()) / 1000
            context: _RetryContext = {'workflow_recovery': True, 'remaining': remaining, 'on_attempt': on_attempt, 'on_deadline': on_deadline}
            if max_retries is not None:
                context['max_retries'] = max_retries
            if sleep_ms is not None:
                context['sleep'] = lambda seconds: sleep_ms(seconds * 1000)
            if random is not None:

                def draw() -> float:
                    sample = random()
                    if type(sample) not in (int, float) or not math.isfinite(sample) or (not 0 <= sample <= 1):
                        raise _invalid('The batch random hook must return a number between zero and one.')
                    return sample
                context['random'] = draw
            if time.monotonic() * 1000 + timeout_ms > deadline or now_ms() + timeout_ms >= admission['admitted_at_ms'] + REPLAY_TTL_MS:
                failure = APIError('The batch deadline was reached.', code='system_zero_timeout')
                deadline_stop = True
            else:
                token = _retry_context.set(context)
                try:
                    response = client.decide(plan['request'], idempotency_key=admission['idempotency_key'])
                except SystemZeroError as error:
                    failure = error
                except Exception:
                    failure = APIError('The batch call did not return a usable response.', code='service_unavailable')
                finally:
                    _retry_context.reset(token)
            code: Optional[str] = None
            outcome = 'completed'
            usage: Optional[dict[str, int]] = None
            questions = []
            if failure is not None:
                code = failure.code if failure.code in _KNOWN_CODES else 'service_unavailable'
                outcome = 'held' if failure.status == 404 and code in _HELD else 'refused' if failure.status == 422 and code in _REFUSALS else 'error'
                if outcome == 'refused':
                    questions = score_refusal(plan['request'], plan['expected'], code)
            else:
                try:
                    questions = score_reply(plan['request'], plan['expected'], response)
                    usage = _reported_usage(response)
                except SystemZeroError:
                    outcome, code = ('error', 'service_unavailable')
            return ({**admission, 'outcome': outcome, 'code': code, 'attempts': attempts, 'latency_ms': max(0.0, time.monotonic() * 1000 - begin), 'usage': usage, 'questions': questions}, deadline_stop)
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            running = {}
            while pending or running:
                while pending and len(running) < concurrency and (stopped not in ('deadline', 'call_limit')):
                    identifier = pending[0]
                    if identifier not in admissions and len(admissions) >= max_calls:
                        stopped = 'call_limit'
                        break
                    if time.monotonic() * 1000 + timeout_ms > deadline:
                        stopped = 'deadline'
                        break
                    if identifier not in admissions:
                        admission = {**prepared[identifier]['admission'], 'admitted_at_ms': int(now_ms())}
                        _append(journal, admission)
                        admissions[identifier] = admission
                    stopped = None
                    pending.pop(0)
                    running[pool.submit(execute, identifier)] = identifier
                if not running:
                    break
                completed, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in completed:
                    identifier = running.pop(future)
                    record, deadline_stop = future.result()
                    _append(path, record)
                    finished[identifier] = record
                    if deadline_stop:
                        stopped = 'deadline'
        return {'records': [finished[identifier] for identifier in prepared if identifier in finished], 'stopped': stopped, 'decide_calls': len(admissions)}
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, OverflowError) as error:
        raise _invalid('The batch receipt files could not be safely read or written.') from error
    finally:
        if owned:
            try:
                lock.unlink()
            except OSError:
                pass
