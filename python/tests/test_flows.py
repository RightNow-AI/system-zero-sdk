"""SYNTHETIC shared flow conformance; every HTTP call uses a mock transport."""
from copy import deepcopy
import json
from pathlib import Path
import httpx
import pytest
from system_zero import SystemZero
from system_zero.errors import APIConnectionError, APIError, APITimeoutError, InvalidRequestError, SystemZeroError, VideoNotAvailableError
from system_zero.flows import flow_idempotency_key, flow_request_hash, run_flow, validate_flow
FIXTURES = json.loads((Path(__file__).resolve().parents[2] / 'fixtures' / 'flows' / 'conformance.json').read_text(encoding='utf-8'))['cases']
BOUNDARIES = json.loads((Path(__file__).resolve().parents[2] / 'fixtures' / 'flows' / 'boundaries.json').read_text(encoding='utf-8'))
QUESTION = {'ok': {'type': 'boolean', 'instructions': 'SYNTHETIC question'}}

def definition(count=1):
    return {'version': 1, 'request': {'model': 'system-zero', 'state': {'note': 'SYNTHETIC input'}}, 'steps': [{'id': 'step' + str(index), 'questions': deepcopy(QUESTION), 'next': {'rules': [], 'default': 'step' + str(index + 1) if index + 1 < count else None}} for index in range(count)]}

def answer(value=True, confidence=0.8):
    return {'type': 'boolean', 'probability': confidence if value else 1 - confidence, 'decision': value, 'confidence': confidence, 'alternatives': [{'decision': not value, 'confidence': round(1 - confidence, 6)}], 'calibrated': False, 'calibration_version': 'synthetic-v1'}

def reply(value=True, confidence=0.8):
    return {'id': 'szd_synthetic', 'model': 'system-zero', 'served_by': 'synthetic', 'created': 1, 'context': {'route': 'direct', 'tokens': 1}, 'answers': {'ok': answer(value, confidence)}, 'usage': {'input_tokens': 1, 'cached_input_tokens': 0, 'media_tokens': 0, 'long_input_tokens': 0, 'media_items': 0, 'thought_tokens': 1}}

def options(**overrides):
    return {'run_id': 'synthetic-run', 'max_decide_calls': 32, 'deadline_ms': 10000, 'include_content': True, 'now': lambda: 0, **overrides}

def client_for(send, **overrides):
    return SystemZero(api_key='rnsk_synthetic', timeout=0.05, max_retries=0, transport=httpx.MockTransport(send), **overrides)

def error_body(code='question_not_grounded', kind='invalid_request_error'):
    return {'request_id': 'req_refusal', 'error': {'type': kind, 'code': code, 'message': 'SYNTHETIC private refusal', 'param': 'questions.*', 'details': {'private': 'SYNTHETIC sensitive'}, 'retryable': False}}

@pytest.mark.parametrize('case', FIXTURES, ids=lambda case: case['name'])
def test_shared_flow_conformance(case):
    if case.get('validate_only'):
        with pytest.raises(InvalidRequestError) as caught:
            validate_flow(case['definition'])
        assert caught.value.code == case['expect']['error_code']
        return
    seen = []
    responses = iter(case.get('responses', []))

    def send(request):
        seen.append(json.loads(request.content))
        assert request.headers['idempotency-key'].startswith('sz-flow-v1-')
        response = next(responses)
        return httpx.Response(response['status'], json=response['body'], headers=response.get('headers'))
    run_options = options(run_id='fixture-run', **case.get('options', {}))
    with SystemZero(api_key='rnsk_synthetic', timeout=case.get('client_timeout_ms', 50) / 1000, max_retries=0, transport=httpx.MockTransport(send)) as client:
        if 'error_code' in case['expect']:
            with pytest.raises(SystemZeroError) as caught:
                run_flow(client, case['definition'], **run_options)
            assert caught.value.code == case['expect']['error_code']
            trace = caught.value.trace
        else:
            trace = run_flow(client, case['definition'], **run_options)
    expected = case['expect']
    assert trace['status'] == expected['status']
    assert [step['step_index'] for step in trace['steps']] == expected['step_indexes']
    if 'step_statuses' in expected:
        assert [step['status'] for step in trace['steps']] == expected['step_statuses']
    if 'branch_rule_indexes' in expected:
        assert [step['branch']['rule_index'] if step['branch'] else None for step in trace['steps']] == expected['branch_rule_indexes']
    if 'requests' in expected:
        assert seen == expected['requests']
    if 'decide_calls' in expected:
        assert trace['decide_calls'] == expected['decide_calls']

@pytest.mark.parametrize('mutation', [lambda flow: flow.update(version=True), lambda flow: flow.update(extra='unknown'), lambda flow: flow['request'].update(extra='unknown'), lambda flow: flow['steps'][0].update(id='bad id'), lambda flow: flow['steps'][1].update(id='step0'), lambda flow: flow['steps'][0].update(questions={}), lambda flow: flow['steps'][0].update(state_template={'state': False}), lambda flow: flow['steps'][0].update(state_template={'literal': 1, 'state': True}), lambda flow: flow['steps'][0].update(next={'rules': []}), lambda flow: flow['steps'][0].update(next={'rules': [], 'default': 'unknown'}), lambda flow: flow['steps'][1]['next'].update(default='step0'), lambda flow: flow['steps'][0].update(depends_on=[{'step': 'step1', 'question': 'ok', 'op': 'eq', 'value': True}]), lambda flow: flow['steps'][1].update(depends_on=[{'step': 'step0', 'question': 'missing', 'op': 'eq', 'value': True}]), lambda flow: flow['steps'][1].update(depends_on=[{'step': 'step0', 'question': 'ok', 'op': 'confidence_gte', 'value': True}]), lambda flow: flow['steps'][1].update(depends_on=[{'step': 'step0', 'question': 'ok', 'op': 'eq', 'value': 1}]), lambda flow: flow['steps'][1].update(state_template={'answer': {'step': 'step0', 'question': 'ok', 'field': 'raw'}})])
def test_definition_validation_rejects_invalid_graph_and_tags(mutation):
    flow = definition(2)
    mutation(flow)
    with pytest.raises(InvalidRequestError) as caught:
        validate_flow(flow)
    assert caught.value.code == 'invalid_request'

@pytest.mark.parametrize('value', [float('nan'), float('inf'), 2 ** 53, object(), {1: 'non-string'}, '\ud800'])
def test_definition_rejects_nonportable_json(value):
    flow = definition()
    flow['request']['state'] = value
    with pytest.raises(InvalidRequestError):
        validate_flow(flow)

def test_definition_rejects_cycles_and_over_32_steps_before_calls():
    circular = definition()
    circular['request']['state'] = circular
    for flow in (circular, definition(33)):
        with pytest.raises(InvalidRequestError):
            validate_flow(flow)

def test_validation_snapshots_json_and_run_is_immune_to_caller_mutation():
    flow = definition(2)
    snapshot = validate_flow(flow)
    assert snapshot == flow and snapshot is not flow
    seen = []

    def send(request):
        seen.append(json.loads(request.content))
        flow['request']['state']['note'] = 'changed after run began'
        flow['steps'][1]['questions']['ok']['instructions'] = 'changed'
        return httpx.Response(200, json=reply())
    with client_for(send) as client:
        run_flow(client, flow, **options())
    assert seen[0] == seen[1]
    assert seen[1]['state'] == {'note': 'SYNTHETIC input'}

def test_templates_are_explicit_and_carry_current_state_and_answer_fields():
    flow = definition(2)
    flow['steps'][1]['state_template'] = {'object': {'original': {'state': True}, 'decision': {'answer': {'step': 'step0', 'question': 'ok', 'field': 'decision'}}, 'confidence': {'answer': {'step': 'step0', 'question': 'ok', 'field': 'confidence'}}, 'alternatives': {'answer': {'step': 'step0', 'question': 'ok', 'field': 'alternatives'}}, 'literal': {'literal': {'answer': '${step0.ok}', 'state': True}}, 'list': {'array': [{'literal': 1}, {'literal': '{{step0.ok}}'}]}}}
    seen = []
    with client_for(lambda request: (seen.append(json.loads(request.content)), httpx.Response(200, json=reply()))[1]) as client:
        trace = run_flow(client, flow, **options())
    assert seen[1]['state'] == {'original': {'note': 'SYNTHETIC input'}, 'decision': True, 'confidence': 0.8, 'alternatives': [{'decision': False, 'confidence': 0.2}], 'literal': {'answer': '${step0.ok}', 'state': True}, 'list': [1, '{{step0.ok}}']}
    assert trace['steps'][1]['answers'][0]['question'] == 'ok'

def test_trace_question_order_is_utf8_sorted_without_changing_wire_order():
    flow = definition()
    flow['steps'][0]['questions'] = {name: deepcopy(QUESTION['ok']) for name in ['2', '10', 'z', 'a']}
    body = reply()
    body['answers'] = {name: answer() for name in flow['steps'][0]['questions']}
    with client_for(lambda _: httpx.Response(200, json=body)) as client:
        trace = run_flow(client, flow, **options())
    assert [item['question'] for item in trace['steps'][0]['answers']] == ['10', '2', 'a', 'z']

def test_request_hash_and_keys_are_order_independent_but_content_sensitive():
    first = {'z': -0.0, 'a': [1, 1.25, True, None, '\U00010000', '\ue000']}
    second = {'a': [1.0, 1.25, True, None, '\U00010000', '\ue000'], 'z': 0}
    assert flow_request_hash(first) == flow_request_hash(second)
    assert flow_idempotency_key('run', 'step', first) == flow_idempotency_key('run', 'step', second)
    assert flow_idempotency_key('other', 'step', first) != flow_idempotency_key('run', 'step', first)
    assert flow_idempotency_key('run', 'other', first) != flow_idempotency_key('run', 'step', first)
    assert flow_request_hash({'a': True}) != flow_request_hash({'a': 1})

@pytest.mark.parametrize('vector', BOUNDARIES['hash_vectors'])
def test_shared_hash_and_idempotency_vectors(vector):
    assert flow_request_hash(vector['value']) == vector['sha256']
    assert flow_idempotency_key('fixture-run', 'start', vector['value']) == vector['idempotency_key']

def test_idempotent_replay_reuses_stored_answers_without_counting_twice():
    stored, calls, counted = ({}, [], [])

    def send(request):
        key = request.headers['idempotency-key']
        calls.append((key, request.content))
        replayed = key in stored
        if not replayed:
            stored[key] = reply()
            counted.append(key)
        return httpx.Response(200, json=stored[key], headers={'x-rightnow-idempotent-replay': str(replayed).lower(), 'x-request-id': 'req_replay'})
    flow = definition(2)
    with client_for(send) as client:
        first = run_flow(client, flow, **options())
        replay = run_flow(client, flow, **options())
    assert calls[:2] == calls[2:]
    assert len(counted) == 2
    assert not any((step['replayed'] for step in first['steps']))
    assert all((step['replayed'] for step in replay['steps']))
    assert replay['decide_calls'] == 2

def test_call_limit_counts_before_dispatch():
    calls = []
    with client_for(lambda request: (calls.append(request), httpx.Response(200, json=reply()))[1]) as client:
        with pytest.raises(SystemZeroError) as caught:
            run_flow(client, definition(2), **options(max_decide_calls=1))
    assert caught.value.code == 'call_limit_reached'
    assert len(calls) == 1
    assert caught.value.trace['decide_calls'] == 1

def test_exact_32_step_boundary_is_bounded_and_every_dispatch_is_counted():
    calls = []
    with client_for(lambda request: (calls.append(request), httpx.Response(200, json=reply()))[1]) as client:
        trace = run_flow(client, definition(32), **options(max_decide_calls=32))
    assert len(calls) == len(trace['steps']) == 32
    assert len({request.headers['idempotency-key'] for request in calls}) == 32
    assert trace['decide_calls'] == 32

def test_sdk_retry_reuses_step_key_and_one_local_call():
    calls = []

    def send(request):
        calls.append(request)
        error = error_body('rate_limit_exceeded', 'rate_limit_error')
        error['error']['retryable'] = True
        error['error']['details'] = {'cause': 'key_rate'}
        return httpx.Response(429, json=error, headers={'retry-after': '0'}) if len(calls) == 1 else httpx.Response(200, json=reply())
    with SystemZero(api_key='rnsk_synthetic', timeout=0.05, max_retries=1, max_retry_wait=0, transport=httpx.MockTransport(send), sleep=lambda _: None, random=lambda: 0) as client:
        trace = run_flow(client, definition(), **options(max_decide_calls=1))
    assert len(calls) == 2
    assert calls[0].content == calls[1].content
    assert calls[0].headers['idempotency-key'] == calls[1].headers['idempotency-key']
    assert trace['decide_calls'] == 1

def test_deadline_includes_sdk_retry_and_wait_bound_before_any_dispatch():
    calls = []
    with SystemZero(api_key='rnsk_synthetic', timeout=0.001, max_retries=2, max_retry_wait=0.005, transport=httpx.MockTransport(lambda request: (calls.append(request), httpx.Response(200, json=reply()))[1])) as client:
        with pytest.raises(APITimeoutError) as caught:
            run_flow(client, definition(), **options(deadline_ms=12))
    assert caught.value.code == 'system_zero_timeout'
    assert calls == []

def test_deadline_rechecked_after_call_without_mutating_client_configuration():
    clock = [0]
    calls = []

    def send(request):
        calls.append(request)
        clock[0] = 100
        return httpx.Response(200, json=reply())
    with client_for(send) as client:
        original = (client.timeout, client.max_retries, client.max_retry_wait)
        with pytest.raises(APITimeoutError) as caught:
            run_flow(client, definition(2), **options(deadline_ms=50, now=lambda: clock[0]))
        assert (client.timeout, client.max_retries, client.max_retry_wait) == original
    assert caught.value.code == 'system_zero_timeout'
    assert len(calls) == 1
    assert caught.value.trace['decide_calls'] == 1

def test_deadline_admission_rechecks_client_configuration_between_calls():
    calls = []

    def send(request):
        calls.append(request)
        client.timeout = 1
        return httpx.Response(200, json=reply())
    with client_for(send) as client:
        with pytest.raises(APITimeoutError) as caught:
            run_flow(client, definition(2), **options(deadline_ms=100))
    assert len(calls) == 1
    assert caught.value.trace['decide_calls'] == 1

def test_http_refusal_mid_run_preserves_typed_error_and_partial_trace():
    responses = iter([httpx.Response(200, json=reply(), headers={'x-request-id': 'req_first'}), httpx.Response(404, json=error_body('video_not_available', 'not_found_error'))])
    with client_for(lambda _: next(responses)) as client:
        with pytest.raises(VideoNotAvailableError) as caught:
            run_flow(client, definition(3), **options())
    assert caught.value.message == 'SYNTHETIC private refusal'
    assert caught.value.details == {'private': 'SYNTHETIC sensitive'}
    assert caught.value.trace['status'] == 'held'
    assert [step['status'] for step in caught.value.trace['steps']] == ['completed', 'held']
    assert caught.value.trace['steps'][0]['request_id'] == 'req_first'
    assert caught.value.trace['decide_calls'] == 2

def test_question_refusal_stops_after_recording_successes_from_same_reply():
    flow = definition(2)
    flow['steps'][0]['questions']['refused'] = {'type': 'boolean', 'instructions': 'SYNTHETIC refused'}
    body = reply()
    body['answers']['refused'] = {'error': {'code': 'question_not_grounded', 'retryable': False}}
    with client_for(lambda _: httpx.Response(200, json=body)) as client:
        with pytest.raises(InvalidRequestError) as caught:
            run_flow(client, flow, **options())
    assert caught.value.code == 'question_not_grounded'
    assert caught.value.trace['status'] == 'refused'
    assert [item['kind'] for item in caught.value.trace['steps'][0]['answers']] == ['v1', 'refusal']

def test_default_trace_does_not_copy_caller_content_or_untrusted_usage_fields():
    flow = definition()
    flow['steps'][0]['id'] = 'private_step'
    flow['steps'][0]['questions'] = {'private_question': {'type': 'boolean', 'instructions': 'private instructions'}}
    flow['request']['state'] = 'private state'
    body = reply()
    body['answers'] = {'private_question': answer()}
    body['usage'].update(extra='private usage', input_tokens='private tokens')
    with client_for(lambda _: httpx.Response(200, json=body)) as client:
        trace = run_flow(client, flow, **options(run_id='private run', include_content=False))
    text = json.dumps(trace)
    assert 'private' not in text
    assert 'decision' not in trace['steps'][0]['answers'][0]
    assert 'alternatives' not in trace['steps'][0]['answers'][0]
    assert trace['steps'][0]['answers'][0]['confidence'] == 0.8

@pytest.mark.parametrize('patch', [lambda body: body.update(answers={}), lambda body: body['answers'].update(extra=answer()), lambda body: body['answers']['ok'].update(confidence=0.5)])
def test_invalid_success_payload_uses_existing_typed_api_error(patch):
    body = reply()
    patch(body)
    with client_for(lambda _: httpx.Response(200, json=body)) as client:
        with pytest.raises(APIError) as caught:
            run_flow(client, definition(), **options())
    assert caught.value.code == 'service_unavailable'
    assert caught.value.trace['status'] == 'failed'

def test_legacy_confidence_stays_unavailable_in_trace_and_conditions():
    flow = definition(2)
    flow['steps'][0]['next'] = {'rules': [{'when': {'step': 'step0', 'question': 'ok', 'op': 'confidence_gte', 'value': 0}, 'next': 'step1'}], 'default': None}
    body = reply()
    body['answers']['ok'] = {'type': 'boolean', 'probability': 0.8, 'confidence': 0.6, 'calibration_version': 'synthetic-legacy'}
    with client_for(lambda _: httpx.Response(200, json=body)) as client:
        trace = run_flow(client, flow, **options())
    assert len(trace['steps']) == 1
    assert trace['steps'][0]['answers'][0]['confidence'] is None
    assert trace['steps'][0]['branch']['reason'] == 'default'

@pytest.mark.parametrize('overrides', [{'run_id': ''}, {'run_id': 'x' * 257}, {'max_decide_calls': True}, {'max_decide_calls': -1}, {'deadline_ms': True}, {'deadline_ms': 0}, {'deadline_ms': 1.5}, {'deadline_ms': float('inf')}, {'include_content': 'yes'}])
def test_invalid_runtime_options_use_typed_error_without_dispatch(overrides):
    calls = []
    with client_for(lambda request: (calls.append(request), httpx.Response(200, json=reply()))[1]) as client:
        with pytest.raises(InvalidRequestError) as caught:
            run_flow(client, definition(), **options(**overrides))
    assert caught.value.code == 'invalid_request'
    assert calls == []

def test_transport_timeout_has_known_code_and_partial_trace():

    def send(request):
        raise httpx.ReadTimeout('SYNTHETIC timeout', request=request)
    with client_for(send) as client:
        with pytest.raises(APIConnectionError) as caught:
            run_flow(client, definition(), **options())
    assert type(caught.value) is APIConnectionError
    assert caught.value.code == 'system_zero_timeout'
    assert caught.value.trace['status'] == 'failed'

def test_body_timeout_retains_known_request_id():

    class InterruptedBody(httpx.SyncByteStream):

        def __iter__(self):
            yield b'{"answers":'
            raise httpx.ReadTimeout('SYNTHETIC timeout during body')
    with client_for(lambda _: httpx.Response(200, headers={'x-request-id': 'req_body_timeout'}, stream=InterruptedBody())) as client:
        with pytest.raises(APIConnectionError) as caught:
            run_flow(client, definition(), **options())
    assert type(caught.value) is APIConnectionError
    assert caught.value.request_id == 'req_body_timeout'
    assert caught.value.trace['steps'][0]['request_id'] == 'req_body_timeout'

def test_unknown_question_refusal_is_typed_service_unavailable():
    body = reply()
    body['answers']['ok'] = {'error': {'code': 'call_limit_reached', 'retryable': False}}
    with client_for(lambda _: httpx.Response(200, json=body, headers={'x-request-id': 'req_invalid_reply'})) as client:
        with pytest.raises(APIError) as caught:
            run_flow(client, definition(), **options())
    assert caught.value.code == 'service_unavailable'
    assert caught.value.retryable is True
    assert caught.value.request_id == 'req_invalid_reply'
    assert caught.value.trace['status'] == 'failed'
    assert 'decision' not in caught.value.trace['steps'][0]['answers'][0]

def test_throwing_clock_uses_typed_invalid_request():

    def broken_clock():
        raise RuntimeError('SYNTHETIC clock failure')
    with client_for(lambda _: httpx.Response(200, json=reply())) as client:
        with pytest.raises(InvalidRequestError):
            run_flow(client, definition(), **options(now=broken_clock))

@pytest.mark.parametrize('code,saved_code', [('SYNTHETIC_private_code', 'service_unavailable')])
def test_flow_trace_preserves_cumulative_public_codes_without_private_content(code, saved_code):
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(409, json=error_body(code))
    with client_for(send) as client:
        with pytest.raises(SystemZeroError) as caught:
            run_flow(client, definition(), **options(include_content=False, max_decide_calls=1))
    error = caught.value
    assert error.code == code
    assert error.details == {'private': 'SYNTHETIC sensitive'}
    assert error.trace['status'] == 'refused'
    assert error.trace['decide_calls'] == 1
    assert error.trace['steps'][0]['error'] == {'code': saved_code, 'type': 'invalid_request_error', 'status': 409, 'retryable': False}
    assert 'SYNTHETIC private' not in json.dumps(error.trace)
    assert 'SYNTHETIC sensitive' not in json.dumps(error.trace)
    assert len(calls) == 1
