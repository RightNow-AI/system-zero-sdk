"""One-item batches keep gateway codes and existing held/refused outcomes."""
import json
import httpx
import pytest
from system_zero import AuthenticationError, InvalidRequestError, NotFoundError, PermissionError, ServerError, SystemZero, SystemZeroError, KNOWN_ERROR_CODES, evaluate, run_batch, run_flow
REQUEST = {'model': 'system-zero', 'state': 'synthetic', 'questions': {'q': {'type': 'boolean', 'instructions': 'Is it set?'}}}
FLOW = {'version': 1, 'request': {'model': REQUEST['model'], 'state': REQUEST['state']}, 'steps': [{'id': 'first', 'questions': REQUEST['questions'], 'next': {'rules': [], 'default': None}}]}
HELD_CODES = {'long_context_not_available', 'long_recording_not_available', 'video_not_available', 'image_not_available', 'audio_not_available', 'document_not_available', 'committee_not_available', 'deliberate_not_available'}
REFUSED_CODES = {'injection_suspected', 'request_refused', 'question_not_grounded'}
OPEN_CODES = ['future_code_x', 'a', 'a' + '0_' * 31 + '0']
MISSING_CODE = object()
MALFORMED_CODES = ['FUTURE_CODE_X', 'future code', 'a' * 65, '', '_future', '1future', 'future-code', 'future.code', 'future_code_x\n', 'future_code_x\r', 'futur\xe9', 123, False, {}, [], None, MISSING_CODE]
WORKFLOW_CODE_CASES = [(code, code, 409) for code in OPEN_CODES] + [(code, 'service_unavailable', 503) for code in MALFORMED_CODES] + [(code, code, status) for code in sorted(KNOWN_ERROR_CODES) for status in (404, 409, 422, 503)]

def test_batch_open_codes_fallback_known_outcomes_and_resume(tmp_path):
    for index, (code, saved_code, status) in enumerate(WORKFLOW_CODE_CASES):
        calls = []

        def send(request):
            calls.append(request)
            error = {'message': 'Synthetic response.', 'type': 'api_error', 'retryable': False}
            if code is not MISSING_CODE:
                error['code'] = code
            return httpx.Response(status, json={'error': error})
        path = tmp_path / f'{index}.jsonl'
        items = [{'id': 'one', 'request': REQUEST, 'expected': {'q': True}}]
        options = {'run_id': 'open-codes', 'results_path': path, 'deadline_ms': 10000, 'concurrency': 1, 'now_ms': lambda: 1000}
        with SystemZero(api_key='synthetic', timeout=1, max_retries=0, transport=httpx.MockTransport(send)) as client:
            result = run_batch(client, items, **options)
            saved = json.loads(path.read_text(encoding='ascii'))
            outcome = 'held' if status == 404 and saved_code in HELD_CODES else 'refused' if status == 422 and saved_code in REFUSED_CODES else 'error'
            assert (saved['code'], saved['outcome'], saved['attempts'], saved['usage']) == (saved_code, outcome, 1, None)
            assert saved['questions'] == ([{'question_id': 'q', 'type': 'boolean', 'outcome': 'refused', 'expected_refusal': False, 'correct': False, 'confidence': None, 'choice_expected': None, 'choice_actual': None, 'choice_schema': None, 'code': saved_code}] if outcome == 'refused' else [])
            assert result['records'] == [saved]
            assert evaluate(result['records'])['records'][0]['code'] == saved_code
            assert run_batch(client, items, **options)['records'] == [saved]
        assert len(calls) == 1

def test_flow_open_codes_fallback_and_known_outcomes():
    for code, saved_code, status in WORKFLOW_CODE_CASES:
        calls = []

        def send(request):
            calls.append(request)
            error = {'message': 'Synthetic response.', 'type': 'api_error', 'retryable': False}
            if code is not MISSING_CODE:
                error['code'] = code
            return httpx.Response(status, json={'error': error})
        with SystemZero(api_key='synthetic', timeout=1, max_retries=0, transport=httpx.MockTransport(send)) as client:
            with pytest.raises(SystemZeroError) as caught:
                run_flow(client, FLOW, run_id='open-codes', deadline_ms=10000, now=lambda: 0)
        trace = caught.value.trace
        outcome = 'held' if saved_code in HELD_CODES else 'refused' if status < 500 else 'failed'
        assert trace['status'] == trace['steps'][0]['status'] == outcome
        assert trace['decide_calls'] == 1
        assert trace['steps'][0]['error'] == {'code': saved_code, 'type': 'api_error', 'status': status, 'retryable': not isinstance(code, str)}
        assert len(calls) == 1

def test_batch_direct_errors_keep_open_codes_and_refuse_malformed_codes(tmp_path):
    for index, code in enumerate(['future_code_x', *MALFORMED_CODES]):

        class FailingClient(SystemZero):

            def decide(self, *args, **kwargs):
                raise SystemZeroError('Synthetic response.', status=503, **{} if code is MISSING_CODE else {'code': code})
        with FailingClient(api_key='synthetic', timeout=1, max_retries=0) as client:
            result = run_batch(client, [{'id': 'one', 'request': REQUEST, 'expected': {'q': True}}], run_id='raised-codes', results_path=tmp_path / f'{index}.jsonl', deadline_ms=10000, concurrency=1)
        row = result['records'][0]
        assert row['code'] == ('future_code_x' if index == 0 else 'service_unavailable')
        assert row['outcome'] == 'error'
        assert row['questions'] == []

def test_flow_direct_errors_keep_open_codes_and_refuse_malformed_codes():
    for index, code in enumerate(['future_code_x', *MALFORMED_CODES]):

        class FailingClient(SystemZero):

            def decide_with_response(self, *args, **kwargs):
                raise SystemZeroError('Synthetic response.', status=503, **{} if code is MISSING_CODE else {'code': code})
        with FailingClient(api_key='synthetic', timeout=1, max_retries=0) as client:
            with pytest.raises(SystemZeroError) as caught:
                run_flow(client, FLOW, run_id='raised-codes', deadline_ms=10000, now=lambda: 0)
        step = caught.value.trace['steps'][0]
        assert step['status'] == 'failed'
        assert step['error']['code'] == ('future_code_x' if index == 0 else 'service_unavailable')

@pytest.mark.parametrize('status,code,error_type,outcome', [(404, 'model_not_found', 'not_found_error', 'error'), (403, 'auth_error', 'authentication_error', 'error'), (403, 'account_suspended', 'permission_error', 'error'), (500, 'idempotency_replayed_error', 'server_error', 'error'), (422, 'request_refused', 'invalid_request_error', 'refused')])
def test_batch_keeps_gateway_code(tmp_path, status, code, error_type, outcome):
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(status, json={'error': {'message': 'Synthetic gateway response.', 'code': code, 'type': error_type, 'retryable': False, 'param': 'model' if code == 'model_not_found' else None}})
    path = tmp_path / 'rows.jsonl'
    request = {'model': 'system-zero', 'state': 'synthetic', 'questions': {'q': {'type': 'boolean', 'instructions': 'Is it set?'}}}
    with SystemZero(api_key='synthetic', base_url='https://api.example.com/v1', timeout=1, max_retries=0, transport=httpx.MockTransport(send)) as client:
        result = run_batch(client, [{'id': 'one', 'request': request, 'expected': {'q': True}}], run_id='gateway-code', results_path=path, deadline_ms=10000, concurrency=1)
    saved = json.loads(path.read_text(encoding='ascii'))
    assert len(calls) == saved['attempts'] == 1
    assert saved['code'] == code
    assert saved['outcome'] == outcome
    assert result['records'][0] == saved
    assert len(saved['questions']) == (1 if outcome == 'refused' else 0)
    if outcome == 'held':
        assert 'cost_nanocents' not in saved and 'accounted_nanocents' not in saved
        assert saved['usage'] is None

@pytest.mark.parametrize('status,code,error_type,error_class', [(403, 'auth_error', 'authentication_error', AuthenticationError), (403, 'account_suspended', 'permission_error', PermissionError), (404, 'model_not_found', 'not_found_error', NotFoundError), (422, 'idempotency_replayed_error', 'invalid_request_error', InvalidRequestError), (503, 'idempotency_replayed_error', 'server_error', ServerError)])
def test_enabled_batch_retries_keep_exact_gateway_error(tmp_path, status, code, error_type, error_class):
    calls, observed, waits = ([], [], [])

    class RecordingSystemZero(SystemZero):

        def decide(self, *args, **kwargs):
            try:
                return super().decide(*args, **kwargs)
            except SystemZeroError as error:
                observed.append(error)
                raise

    def send(request):
        calls.append(request)
        return httpx.Response(status, json={'request_id': 'req_batch_code', 'error': {'message': 'Synthetic gateway response.', 'code': code, 'type': error_type, 'retryable': True}}, headers={'x-should-retry': 'true', 'retry-after-ms': '1'})
    path = tmp_path / 'rows.jsonl'
    request = {'model': 'system-zero', 'state': 'synthetic', 'questions': {'q': {'type': 'boolean', 'instructions': 'Is it set?'}}}
    with RecordingSystemZero(api_key='synthetic', base_url='https://api.example.com/v1', timeout=1, max_retries=3, sleep=waits.append, transport=httpx.MockTransport(send)) as client:
        result = run_batch(client, [{'id': 'one', 'request': request, 'expected': {'q': True}}], run_id='gateway-code-retries', results_path=path, deadline_ms=10000, concurrency=1, max_retries=3, sleep_ms=waits.append)
    assert len(calls) == len(observed) == 1
    assert waits == []
    error = observed[0]
    assert type(error) is error_class
    assert (error.code, error.status, error.type, error.request_id) == (code, status, error_type, 'req_batch_code')
    saved = json.loads(path.read_text(encoding='ascii'))
    assert (saved['code'], saved['outcome'], saved['attempts']) == (code, 'error', 1)
    assert result['records'] == [saved]
