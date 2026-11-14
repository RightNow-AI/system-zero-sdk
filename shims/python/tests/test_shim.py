import importlib
import hashlib
import hmac
import json
from pathlib import Path
import sys
import warnings
import httpx
import pytest


def test_compatibility_package(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / 'src'))
    import rightnow
    monkeypatch.delenv('RIGHTNOW_API_KEY', raising=False)
    monkeypatch.setenv('SYSTEM_ZERO_API_KEY', 'old-key')
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        import system_zero
        import system_zero.stream
        import system_zero.errors
        importlib.reload(system_zero)
    assert len(caught) == 1
    assert caught[0].category is DeprecationWarning
    assert 'rightnow-sdk' in str(caught[0].message)
    assert system_zero.SystemZero is system_zero.RightNow
    assert issubclass(system_zero.SystemZero, rightnow.RightNow)
    assert system_zero.stream.SystemZeroStreamClient is system_zero.SystemZeroStreamClient
    assert system_zero.STREAM_PROTOCOL == system_zero.stream.STREAM_PROTOCOL == 'system-zero.stream.v1'
    assert system_zero.errors.SystemZeroError is rightnow.RightNowError
    with system_zero.SystemZero() as client:
        assert client._wire.name == 'system-zero'
        assert client.idempotency == 'auto'
    assert 'rightnow-sdk==0.5.1' in (root / 'pyproject.toml').read_text(encoding='utf-8')


def test_all_clients_keep_legacy_wire(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'src'))
    import system_zero
    from system_zero import client as module, stream
    request_body = {'model': 'system-zero', 'state': 'ready', 'questions': {'q': {'type': 'boolean', 'instructions': 'Ready?'}}}
    def respond(request):
        assert str(request.url) == 'https://api.rightnowai.co/v1/system-zero/decide'
        assert request.headers['user-agent'].startswith('system-zero-python/0.5.1 python/')
        assert request.headers['sz-danger-zone'] == 'accept'
        assert 'rightnow-danger-zone' not in request.headers
        return httpx.Response(200, json={})
    def timeout(request):
        raise httpx.ReadTimeout('timeout')
    for Client in (system_zero.RightNow, system_zero.SystemZero, module.RightNow, module.SystemZero):
        with Client(api_key='test-key', transport=httpx.MockTransport(respond)) as client:
            assert client._wire.name == 'system-zero'
            client.decide(request_body, danger_zone='accept')
        with Client(api_key='test-key', max_retries=0, transport=httpx.MockTransport(timeout)) as client:
            with pytest.raises(Exception) as caught:
                client.decide(request_body)
            assert caught.value.code == 'system_zero_timeout'
    for Client in (system_zero.RightNowStreamClient, system_zero.SystemZeroStreamClient, stream.RightNowStreamClient, stream.SystemZeroStreamClient, system_zero.stream_client, stream.stream_client):
        client = Client(api_key='test-key')
        assert client._wire.name == 'system-zero'
        assert client.stream_url == 'wss://api.rightnowai.co/v1/system-zero/stream'
        assert client.idempotency == 'auto'


def test_legacy_tool_factories_own_legacy_clients(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'src'))
    import rightnow
    try:
        current_tools = importlib.import_module('rightnow.tools')
    except ModuleNotFoundError as error:
        if error.name != 'rightnow.tools':
            raise
        # Packaging stages this source adapter as rightnow.tools.
        monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / 'integrations/function-tools'))
        current_tools = importlib.import_module('system_zero_tools')
        monkeypatch.setitem(sys.modules, 'rightnow.tools', current_tools)
        monkeypatch.setattr(rightnow, 'tools', current_tools, raising=False)
    from system_zero import tools
    calls = []

    def respond(request):
        calls.append(request)
        assert str(request.url) == 'https://api.rightnowai.co/v1/models'
        assert request.headers['user-agent'].startswith('system-zero-python/0.5.1 python/')
        return httpx.Response(200, json={'object': 'list', 'data': []})

    monkeypatch.setattr(current_tools, 'SystemZero', lambda **options: rightnow.RightNow(
        api_key='test-key', transport=httpx.MockTransport(respond), **options))
    for factory in (tools.ToolHandler, tools.create_tool_handler):
        with factory() as handler:
            assert handler('system_zero_list_models', {})['status'] == 'ok'
    assert len(calls) == 2


def test_webhooks_keep_legacy_paths():
    import system_zero
    calls = []

    def respond(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={})

    with system_zero.RightNow(api_key='test-key', transport=httpx.MockTransport(respond)) as client:
        client.webhooks.list()
        client.webhooks.create({'url': 'https://example.com/events', 'events': ['decision.refused']})
        client.webhooks.rotate_secret('wh_' + '1' * 32)
    assert calls == [('GET', '/v1/system-zero/webhooks'), ('POST', '/v1/system-zero/webhooks'),
                     ('POST', '/v1/system-zero/webhooks/wh_' + '1' * 32 + '/rotate-secret')]


@pytest.mark.parametrize('model,code', [('rightnow-decision-3.0.0', 'model_unavailable'),
                                       ('system-zero-3.0.0', 'system_zero_unavailable')])
def test_shim_verifies_both_webhook_name_families(model, code):
    import system_zero
    secret, timestamp = 'whsec_' + 'ab' * 32, 1770000000
    body = json.dumps({'id': 'evt_names', 'object': 'webhook_event', 'type': 'decision.refused',
                       'created': timestamp, 'data': {'model': model, 'refusal_code': code}}).encode()
    digest = hmac.new(bytes.fromhex(secret[6:]), str(timestamp).encode() + b'.' + body, hashlib.sha256).hexdigest()
    assert system_zero.verify_webhook_signature(body, f't={timestamp},v1={digest}', secret, now=timestamp)
    assert json.loads(body)['data'] == {'model': model, 'refusal_code': code}
