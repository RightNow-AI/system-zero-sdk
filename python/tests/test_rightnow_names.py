import importlib
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest


REQUEST = {'model': 'rightnow-decision', 'state': 'ready', 'questions': {'q': {'type': 'boolean', 'instructions': 'Ready?'}}}


def test_package_and_shim_offer_their_stream_pair():
    script = """
import asyncio, json, sys
sys.path.insert(0, sys.argv[1])
from rightnow import RightNowStreamClient, stream
from system_zero.stream import RightNowStreamClient as Shim
offered = []
async def connect(url, **options):
    offered.append((url, options['subprotocols']))
    raise ConnectionRefusedError('synthetic disconnect')
stream.connect = connect
async def scenario():
    for Client in (RightNowStreamClient, Shim):
        client = Client(api_key='test-key', max_reconnect_attempts=1)
        try:
            try:
                await client.connect()
            except Exception:
                pass
        finally:
            await client.close()
asyncio.run(scenario())
print(json.dumps(offered))
"""
    result = subprocess.run([sys.executable, '-B', '-c', script,
        str(Path(__file__).resolve().parents[2] / 'shims/python/src')],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        ['wss://api.rightnowai.co/v1/decision-model/stream', ['rightnow-decision.stream.v1']],
        ['wss://api.rightnowai.co/v1/system-zero/stream', ['system-zero.stream.v1']],
    ]


def test_new_decide_path_model_and_class_header():
    from rightnow import RightNow
    seen = []
    def respond(request):
        seen.append(request)
        assert str(request.url) == 'https://api.rightnowai.co/v1/decision-model/decide'
        assert request.method == 'POST'
        assert json.loads(request.content)['model'] == 'rightnow-decision'
        assert request.headers['RightNow-Danger-Zone'] == 'long_context_64k'
        assert 'SZ-Danger-Zone' not in request.headers
        return httpx.Response(200, json={})
    with RightNow(api_key='test-key', transport=httpx.MockTransport(respond)) as client:
        client.decide(REQUEST, danger_zone='long_context_64k')
    assert len(seen) == 1


def test_new_class_answer_precedes_legacy_fallback():
    from rightnow import RightNow
    for headers, expected in [
        ({'rightnow-danger-zone': 'long_context_64k', 'sz-danger-zone': 'accept'}, 'long_context_64k'),
        ({'sz-danger-zone': 'long_context_1m'}, 'long_context_1m'),
    ]:
        with RightNow(api_key='test-key', transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={}, headers=headers))) as client:
            assert client.decide_with_response(REQUEST).metadata.danger_zone == [expected]


def test_shim_decide_path_model_and_class_header():
    script = '''
import json, sys, warnings
import httpx
sys.path.insert(0, sys.argv[1])
warnings.simplefilter('ignore', DeprecationWarning)
import system_zero as shim
seen = []
def respond(request):
    seen.append(request)
    assert str(request.url) == 'https://api.rightnowai.co/v1/system-zero/decide'
    assert request.method == 'POST'
    assert json.loads(request.content)['model'] == 'system-zero'
    assert request.headers['SZ-Danger-Zone'] == 'long_context_64k'
    assert 'RightNow-Danger-Zone' not in request.headers
    return httpx.Response(200, json={})
with shim.SystemZero(api_key='test-key', transport=httpx.MockTransport(respond)) as client:
    client.decide({'model': 'system-zero', 'state': 'ready',
                   'questions': {'q': {'type': 'boolean', 'instructions': 'Ready?'}}},
                  danger_zone='long_context_64k')
assert len(seen) == 1
'''
    result = subprocess.run([
        sys.executable, '-B', '-c', script,
        str(Path(__file__).resolve().parents[2] / 'shims/python/src'),
    ], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_new_names_and_identical_aliases():
    sdk = importlib.import_module('rightnow')
    assert sdk.RightNow is sdk.SystemZero
    assert sdk.RightNowStreamClient is sdk.SystemZeroStreamClient
    assert sdk.RightNowError is sdk.SystemZeroError


@pytest.mark.parametrize('explicit,current,expected', [
    (None, None, 'legacy-key'), (None, 'new-key', 'new-key'),
    ('explicit-key', 'new-key', 'explicit-key'),
])
def test_key_precedence(monkeypatch, explicit, current, expected):
    from rightnow import RightNow, RightNowStreamClient
    from rightnow._redaction import reveal
    monkeypatch.setenv('SYSTEM_ZERO_API_KEY', 'legacy-key')
    if current is None:
        monkeypatch.delenv('RIGHTNOW_API_KEY', raising=False)
    else:
        monkeypatch.setenv('RIGHTNOW_API_KEY', current)
    def respond(request):
        assert request.headers['authorization'] == 'Bearer ' + expected
        return httpx.Response(200, json={'object': 'list', 'data': []})
    with RightNow(api_key=explicit, transport=httpx.MockTransport(respond)) as client:
        client.models.list()
    stream = RightNowStreamClient(api_key=explicit)
    assert reveal(stream._api_key) == expected


def test_python_shim_modules_warning_and_dependency():
    root = Path(__file__).resolve().parents[2]
    script = '''
import importlib, sys, warnings
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter('always')
    import system_zero, rightnow
    import system_zero.stream
    importlib.import_module('system_zero.errors')
    assert system_zero.SystemZero is system_zero.RightNow
    assert issubclass(system_zero.RightNow, rightnow.RightNow)
    assert system_zero.RightNow._wire.name == 'system-zero'
    assert system_zero.stream.SystemZeroStreamClient is system_zero.RightNowStreamClient
    assert issubclass(system_zero.RightNowStreamClient, rightnow.RightNowStreamClient)
    assert system_zero.RightNowStreamClient._wire.name == 'system-zero'
    assert len(caught) == 1
    assert caught[0].category is DeprecationWarning
    assert 'rightnow-sdk' in str(caught[0].message)
'''
    result = subprocess.run([sys.executable, '-c', script, str(root / 'shims/python/src'), str(root / 'python/src')], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'rightnow-sdk==0.5.1' in (root / 'shims/python/pyproject.toml').read_text()
