"""SECURITY-SWEEP-1026: synthetic credentials, mocked transports only."""
import asyncio
import json
import logging
import pickle
import secrets
import socket
import traceback
import time
from collections.abc import Mapping

import httpx
import pytest
from system_zero import SystemZero
from system_zero import stream as stream_module

LOOPBACK = '.'.join(map(str, (127, 0, 0, 1)))
KEY = 'sk-sentinel-' + secrets.token_hex(16)
STATUSES = [401, 403, 404, 409, 413, 422, 429, *range(500, 600)]

def headers():
    return {'Authorization': 'Bearer ' + KEY, 'Proxy-Authorization': 'Basic ' + KEY, 'x-request-id': KEY, 'x-debug': KEY}

def echo():
    return {'request_id': KEY, 'error': {'type': 'api_error', 'code': KEY, 'param': KEY,
        'message': 'refused ' + KEY, 'retryable': False,
        'details': {'nested': [{'Authorization': 'Bearer ' + KEY, 'note': KEY}], KEY: KEY}}}

def surfaces(error):
    seen = set()
    def scan(value):
        if id(value) in seen:
            return '[cycle]'
        seen.add(id(value))
        if isinstance(value, BaseException):
            return {'repr': repr(value), 'str': str(value), 'attrs': scan(vars(value)),
                    'args': scan(value.args), 'cause': scan(value.__cause__), 'context': scan(value.__context__)}
        if isinstance(value, Mapping):
            return {str(k): scan(v) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return [scan(v) for v in value]
        return repr(value)
    return json.dumps(scan(error)) + ''.join(traceback.format_exception(error))

def clean(error, client, caplog, capsys):
    assert isinstance(error, BaseException)
    captured = capsys.readouterr()
    text = surfaces(error) + repr(client) + str(client) + caplog.text + captured.out + captured.err
    assert KEY not in text, 'sentinel present in a diagnostic surface'
    h = getattr(error, 'headers', {})
    assert not any(str(k).lower() in ('authorization', 'proxy-authorization') for k in h)
    assert 'authorization' not in repr(getattr(error, 'details', {})).lower()
    try:
        encoded = pickle.dumps(error)
    except (TypeError, pickle.PicklingError) as failure:
        assert KEY not in str(failure)
    else:
        assert KEY.encode() not in encoded
        assert KEY not in surfaces(pickle.loads(encoded))

@pytest.mark.parametrize('status', STATUSES)
def test_http_echo(status, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    def handler(request):
        assert request.headers['authorization'] == 'Bearer ' + KEY
        return httpx.Response(status, json=echo(), headers=headers())
    with SystemZero(KEY, max_retries=0, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(Exception) as caught:
            client.models.list()
        clean(caught.value, client, caplog, capsys)

@pytest.mark.parametrize('kind', ['network', 'dns', 'timeout', 'wrapped', 'malformed', 'truncated', 'programmer', 'deadline', 'group', 'hooks'])
def test_transport_failure(kind, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{'
            raise httpx.ReadError(KEY)
    def handler(request):
        if kind == 'hooks':
            class NativeFailure(Exception):
                def __str__(self): return KEY
                def __repr__(self): return KEY
                def __reduce__(self): return (Exception, (KEY,))
            raise NativeFailure('synthetic')
        if kind == 'group':
            raise ExceptionGroup(KEY, [ValueError(KEY), ExceptionGroup('nested', [OSError(5, KEY)])])
        if kind == 'programmer':
            raise ValueError(KEY)
        if kind == 'deadline':
            time.sleep(0.005)
            return httpx.Response(200, json={'ok': True}, headers=headers())
        if kind == 'malformed':
            return httpx.Response(200, content=('{"' + KEY + '":').encode(), headers=headers())
        if kind == 'truncated':
            return httpx.Response(200, stream=Broken(), headers=headers())
        if kind == 'dns':
            raise socket.gaierror(socket.EAI_NONAME, KEY)
        if kind == 'timeout':
            raise httpx.ReadTimeout(KEY, request=request)
        if kind == 'wrapped':
            try:
                raise httpx.ConnectError(KEY, request=request)
            except httpx.ConnectError as cause:
                raise RuntimeError('wrapper ' + KEY) from cause
        raise httpx.ConnectError(KEY, request=request)
    with SystemZero(KEY, max_retries=0, timeout=0.001 if kind == 'deadline' else 5, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(Exception) as caught:
            client.models.list()
        clean(caught.value, client, caplog, capsys)

def test_retry_metadata(caplog, capsys):
    counts, waits = [], []
    def handler(request):
        counts.append(request.headers['x-sdk-retry-count'])
        body = echo()
        body['error'].update(code='service_unavailable', retryable=True)
        return httpx.Response(503, json=body, headers={**headers(), 'retry-after-ms': '1'})
    with SystemZero(KEY, max_retries=2, sleep=waits.append, random=lambda: 0, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(Exception) as caught:
            client.models.list()
        clean(caught.value, client, caplog, capsys)
    assert counts == ['0', '1', '2']
    assert len(waits) == 2

def test_client_printing_and_pickling():
    with SystemZero(KEY) as client:
        assert KEY not in repr(client) + str(client) + repr(vars(client))
        try:
            encoded = pickle.dumps(client)
        except (TypeError, pickle.PicklingError) as failure:
            assert KEY not in str(failure)
        else:
            assert KEY.encode() not in encoded

def test_error_pickle_roundtrip():
    with SystemZero(KEY, max_retries=0, transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json=echo(), headers=headers()))) as client:
        with pytest.raises(Exception) as caught:
            client.models.list()
        data = pickle.dumps(caught.value)
        assert KEY.encode() not in data
        restored = pickle.loads(data)
        assert type(restored) is type(caught.value)
        assert restored.status == 401
        assert KEY not in surfaces(restored)

def test_stream_disconnect(monkeypatch, caplog, capsys):
    async def scenario():
        async def connect(_url, **options):
            assert options['additional_headers']['Authorization'] == 'Bearer ' + KEY
            raise ConnectionResetError(KEY)
        monkeypatch.setattr(stream_module, 'connect', connect)
        client = stream_module.SystemZeroStreamClient(KEY, max_reconnect_attempts=1)
        try:
            with pytest.raises(Exception) as caught:
                await asyncio.wait_for(client.connect(), 2)
            clean(caught.value, client, caplog, capsys)
        finally:
            await client.close()
    asyncio.run(scenario())

def test_websocket_debug_headers(monkeypatch, caplog, capsys):
    caplog.set_level(logging.DEBUG, logger='websockets.client')
    async def scenario():
        async def connect(_url, **options):
            # Same logger call as websockets' handshake implementation.
            logger = options.get('logger', logging.getLogger('websockets.client'))
            for name, value in options['additional_headers'].items():
                logger.debug('> %s: %s', name, value)
            raise ConnectionResetError('synthetic disconnect')
        monkeypatch.setattr(stream_module, 'connect', connect)
        client = stream_module.SystemZeroStreamClient(KEY, max_reconnect_attempts=1)
        try:
            with pytest.raises(Exception) as caught:
                await asyncio.wait_for(client.connect(), 2)
            clean(caught.value, client, caplog, capsys)
            assert 'Bearer [redacted]' in caplog.text
        finally:
            await client.close()
    asyncio.run(scenario())

@pytest.mark.parametrize('status', STATUSES)
def test_stream_upgrade_echo(status, monkeypatch, caplog, capsys):
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response
    from websockets.datastructures import Headers
    async def scenario():
        async def connect(_url, **_options):
            raise InvalidStatus(Response(status, 'synthetic', Headers(headers()), json.dumps(echo()).encode()))
        monkeypatch.setattr(stream_module, 'connect', connect)
        client = stream_module.SystemZeroStreamClient(KEY, max_reconnect_attempts=1)
        try:
            with pytest.raises(Exception) as caught:
                await asyncio.wait_for(client.connect(), 2)
            clean(caught.value, client, caplog, capsys)
        finally:
            await client.close()
    asyncio.run(scenario())

def test_connected_stream_disconnect(monkeypatch, caplog, capsys):
    from key_hygiene_fixture import Socket, REQUEST
    async def scenario():
        socket_ = Socket()
        async def connect(_url, **_options):
            return socket_
        monkeypatch.setattr(stream_module, 'connect', connect)
        client = stream_module.SystemZeroStreamClient(KEY, max_reconnect_attempts=1, reconnect_policy='never')
        try:
            await client.connect()
            ack = await client.start(REQUEST)
            waiter = asyncio.create_task(client.wait_for_decision(ack))
            socket_.incoming.put_nowait(ConnectionResetError(KEY))
            with pytest.raises(Exception) as caught:
                await asyncio.wait_for(waiter, 2)
            clean(caught.value, client, caplog, capsys)
        finally:
            await client.close()
    asyncio.run(scenario())

def test_actual_websocket_debug_output(caplog, capsys):
    from websockets.asyncio.server import serve
    caplog.set_level(logging.DEBUG, logger='websockets.client')
    server_logger = logging.Logger('synthetic-server', level=logging.CRITICAL)
    async def handler(socket_):
        await socket_.send('{"' + KEY + '":')
        await socket_.close()
    async def scenario():
        async with serve(handler, LOOPBACK, 0, logger=server_logger) as server:
            port = server.sockets[0].getsockname()[1]
            client = stream_module.SystemZeroStreamClient(KEY, stream_url='ws://' + LOOPBACK + f':{port}', max_reconnect_attempts=1)
            try:
                with pytest.raises(Exception) as caught:
                    await asyncio.wait_for(client.connect(), 2)
                clean(caught.value, client, caplog, capsys)
                assert 'Bearer [redacted]' in caplog.text
            finally:
                await client.close()
    asyncio.run(scenario())

def test_default_http_transport_debug_echo(caplog, capsys):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    caplog.set_level(logging.DEBUG)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.headers['authorization'] == 'Bearer ' + KEY
            body = json.dumps(echo()).encode()
            self.send_response(401)
            for name, value in headers().items():
                self.send_header(name, value)
            self.send_header('content-length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer((LOOPBACK, 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with SystemZero(KEY, base_url='http://' + LOOPBACK + f':{server.server_port}', max_retries=0) as client:
            with pytest.raises(Exception) as caught:
                client.models.list()
            clean(caught.value, client, caplog, capsys)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)

def test_connected_stream_error_frame(monkeypatch, caplog, capsys):
    from key_hygiene_fixture import Socket, REQUEST
    async def scenario():
        socket_ = Socket()
        async def connect(_url, **_options):
            return socket_
        monkeypatch.setattr(stream_module, 'connect', connect)
        client = stream_module.SystemZeroStreamClient(KEY, max_reconnect_attempts=1)
        try:
            await client.connect()
            events = client.events()
            ack = await client.start(REQUEST)
            waiter = asyncio.create_task(client.wait_for_decision(ack))
            socket_.server('error', phase='decide', state_version=1, event_id=None, fatal=True, http_status=503,
                           envelope=echo(), retry={'should_retry': False, 'retry_after': KEY, 'retry_after_ms': None})
            with pytest.raises(Exception) as caught:
                await asyncio.wait_for(waiter, 2)
            clean(caught.value, client, caplog, capsys)
            while True:
                event = await asyncio.wait_for(anext(events), 1)
                assert KEY not in repr(event)
                if event['type'] == 'error':
                    break
        finally:
            await client.close()
    asyncio.run(scenario())


def test_review_A01_request_boundary_echo():
    from system_zero.errors import AuthenticationError
    def handler(request):
        return httpx.Response(401, json={'error': {'type': 'authentication_error', 'code': 'auth_error', 'message': KEY}},
                              headers={'x-request-id': KEY, 'x-debug': KEY})
    with SystemZero(KEY, max_retries=0, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AuthenticationError) as caught:
            client.models.list()
        assert KEY not in surfaces(caught.value)
        assert KEY.encode() not in pickle.dumps(caught.value)

def test_review_B01_request_boundary_dependency_debug(caplog):
    caplog.set_level(logging.DEBUG, logger='httpcore.http11')
    def handler(request):
        logging.getLogger('httpcore.http11').debug('echo %s', KEY)
        return httpx.Response(200, json={'object': 'list', 'data': []})
    with SystemZero(KEY, transport=httpx.MockTransport(handler)) as client:
        client.models.list()
    assert KEY not in caplog.text

def _review_native(kind):
    if kind == 'syntax': return SyntaxError(KEY, (KEY, 1, 1, KEY))
    if kind == 'decode': return UnicodeDecodeError('utf-8', KEY.encode(), 0, 1, KEY)
    if kind == 'encode': return UnicodeEncodeError('ascii', KEY, 0, 1, KEY)
    if kind == 'translate': return UnicodeTranslateError(KEY, 0, 1, KEY)
    if kind == 'attribute': return AttributeError('safe', name=KEY, obj={'echo': KEY})
    if kind == 'import': return ImportError('safe', name=KEY, path=KEY)
    failure = ValueError('safe')
    setattr(failure, KEY, 'safe')
    return failure

@pytest.mark.parametrize('kind', ['syntax', 'decode', 'encode', 'translate', 'attribute', 'import', 'attribute-name'])
@pytest.mark.parametrize('boundary', ['sync', 'async'])
def test_review_A04_B04_native_slots_and_exception_graph(kind, boundary):
    from copy import copy, deepcopy
    from system_zero._redaction import Secret, protected, protected_async
    failure = _review_native(kind)
    if hasattr(failure, 'add_note'):
        failure.add_note(KEY)
    else:
        failure.__notes__ = [KEY]
    failure.__cause__ = SyntaxError(KEY)
    failure.__context__ = UnicodeDecodeError('utf-8', KEY.encode(), 0, 1, KEY)
    class Owner:
        _api_key = Secret(KEY)
        @protected
        def sync(self): raise failure
        @protected_async
        async def async_(self): raise failure
    with pytest.raises(Exception) as caught:
        if boundary == 'sync': Owner().sync()
        else: asyncio.run(Owner().async_())
    error = caught.value
    assert KEY not in str(error), 'native str slot'
    assert KEY not in format(error), 'native format slot'
    assert KEY not in surfaces(error), 'exception graph or attribute name'
    for native in (error, error.__cause__, error.__context__):
        if native is None: continue
        for name in ('msg', 'filename', 'text', 'reason', 'object', 'name', 'obj', 'path', '__notes__'):
            assert KEY not in repr(getattr(native, name, None)), name
        for clone in (copy(native), deepcopy(native), pickle.loads(pickle.dumps(native))):
            assert KEY not in surfaces(clone)
        assert KEY.encode() not in pickle.dumps(native)

def test_review_A05_flow_error_trace_metadata():
    from system_zero.flows import run_flow
    flow = {'version': 1, 'request': {'model': 'system-zero', 'state': 'safe'},
            'steps': [{'id': 'first', 'questions': {'ready': {'type': 'boolean', 'instructions': 'Ready?'}},
                       'next': {'rules': [], 'default': None}}]}
    with SystemZero(KEY, max_retries=0, timeout=1, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={}, headers={'x-request-id': KEY}))) as client:
        with pytest.raises(Exception) as caught:
            run_flow(client, flow, run_id='review-flow', deadline_ms=10000, now=lambda: 0)
        assert caught.value.code == 'service_unavailable'
        assert KEY not in caught.value.request_id
        assert KEY not in json.dumps(caught.value.trace)
        assert KEY.encode() not in pickle.dumps(caught.value)

_REVIEW_CLOSE_RESULTS = {}

@pytest.fixture(scope='session', autouse=True)
def _review_close_trace():
    yield
    import os
    from pathlib import Path
    output = os.environ.get('SDK_CLOSE_TRACE_OUTPUT')
    if output:
        Path(output).write_text(json.dumps(_REVIEW_CLOSE_RESULTS, sort_keys=True, indent=2) + '\n')

@pytest.mark.parametrize('code', [1000, 1001, 1006, 1011, 1012, 1013])
@pytest.mark.parametrize('operation', ['mutation', 'decide'])
def test_review_B02_retryable_close_preserves_eventual_receipt(code, operation):
    from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
    from websockets.frames import Close
    async def scenario():
        client = stream_module.SystemZeroStreamClient(KEY)
        client._arm_receipt = lambda future: None
        expected = {'type': 'ack' if operation == 'mutation' else 'scheduled', 'state_version': 1}
        close_kind = ConnectionClosedOK if code in (1000, 1001) else ConnectionClosedError
        reason = 'safe close'
        failure = close_kind(None, None) if code == 1006 else close_kind(Close(code, reason), Close(code, reason), True)
        observed = {}
        class Socket:
            async def send(self, raw): raise failure
        async def send(frame, mutation=None, before=None):
            if before: before()
            future = mutation.future if mutation is not None else client._pending_schedule
            asyncio.get_running_loop().call_soon(lambda: future.set_result(expected) if not future.done() else None)
            try:
                await client._send_raw(Socket(), '{}')
            except Exception as error:
                observed.update(kind=type(error).__name__, text=str(error), args=repr(error.args),
                                received=getattr(getattr(error, 'rcvd', None), 'code', None),
                                sent=getattr(getattr(error, 'sent', None), 'code', None))
                raise
        client._send_mutation = send
        client._send = send
        try:
            if operation == 'mutation':
                mutation = stream_module._Mutation(asyncio.get_running_loop().create_future(), 'evt_1', 1, 'append_text', {'text': 'safe'})
                result = await asyncio.wait_for(client._mutate(mutation), 1)
            else:
                result = await asyncio.wait_for(client._decide_owned(), 1)
            assert json.dumps(result, sort_keys=True) == json.dumps(expected, sort_keys=True)
            _REVIEW_CLOSE_RESULTS[f'{operation}-{code}'] = {
                'receipt': result, 'failure': observed, 'request_sent': client._request_sent,
                'reconnect_requested': client._reconnect_requested.is_set(),
                'explicit_intent': client._explicit_intent,
            }
        finally: await client.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('path, fault, surface', [
    ('send', 'close', 'client'), ('send', 'close', 'future'),
    ('receive', 'close', 'client'), ('receive', 'close', 'task'),
    ('receive', 'reset', 'client'), ('receive', 'reset', 'task'),
    ('native-pong', 'close', 'client'), ('native-pong', 'close', 'future'), ('native-pong', 'close', 'task'),
    ('native-pong', 'reset', 'client'), ('native-pong', 'reset', 'future'), ('native-pong', 'reset', 'task'),
    ('json-pong', 'close', 'client'), ('json-pong', 'close', 'future'),
    ('probe', 'close', 'client'), ('probe', 'close', 'task'),
    ('probe', 'reset', 'client'), ('probe', 'reset', 'task'),
])
def test_review_round3_retained_stream_failure(path, fault, surface, monkeypatch):
    from key_hygiene_fixture import Socket, REQUEST
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close, Frame, OP_PONG
    async def scenario():
        armed, faulted = asyncio.Event(), asyncio.Event()
        def failure():
            return (ConnectionClosedError(Close(1011, KEY), Close(1011, KEY), True)
                    if fault == 'close' else ConnectionResetError(KEY))
        class Peer(Socket):
            async def send(self, raw):
                kind = json.loads(raw)['type']
                if armed.is_set() and ((path == 'send' and kind == 'append_text') or
                                       (path == 'json-pong' and kind == 'pong')):
                    faulted.set()
                    raise failure()
                return await super().send(raw)
            async def ping(self):
                await armed.wait()
                faulted.set()
                raise failure()
        socket_ = Peer(native=path == 'probe')
        if path == 'probe':
            hello = json.loads(socket_.incoming.get_nowait())
            hello['limits']['native_heartbeat_interval_ms'] = 1
            socket_.incoming.put_nowait(json.dumps(hello))
        if path == 'native-pong':
            class Protocol:
                def send_frame(self, frame):
                    faulted.set()
                    raise failure()
            socket_.protocol = Protocol()
        async def connect(_url, **_options): return socket_
        monkeypatch.setattr(stream_module, 'connect', connect)
        client = stream_module.SystemZeroStreamClient(KEY, max_reconnect_attempts=1, reconnect_policy='never')
        pending = None
        watcher = None
        samples = []
        completed = []
        try:
            await client.connect()
            await client.start(REQUEST)
            socket_.close_release.clear()
            retained_future = client._transport_failure
            retained_tasks = [client._reader]
            stop = asyncio.Event()
            async def observe():
                while not stop.is_set():
                    if faulted.is_set():
                        value = repr(vars(client)) if surface == 'client' else repr(retained_future) if surface == 'future' else repr(retained_tasks)
                        samples.append(value)
                        completed.append(retained_future.done() if surface == 'future' else any(t.done() for t in retained_tasks))
                    await asyncio.sleep(0)
            watcher = asyncio.create_task(observe())
            armed.set()
            if path == 'receive':
                faulted.set()
                socket_.incoming.put_nowait(failure())
            elif path == 'send':
                pending = asyncio.create_task(client.append_text('safe'))
            elif path == 'native-pong':
                socket_.protocol.send_frame(Frame(OP_PONG, b'safe'))
                retained_tasks = list(client._native_tasks)
            elif path == 'json-pong':
                socket_.server('ping', nonce=1)
            await asyncio.wait_for(faulted.wait(), 2)
            for _ in range(96): await asyncio.sleep(0)
            stop.set()
            await watcher
            assert len(samples) >= 32, 'sample the close race across event-loop turns'
            assert any(completed), 'sample a completed retained Future or Task'
            assert all(KEY not in value for value in samples), 'retained client/Future/Task diagnostics contain the credential'
        finally:
            socket_.close_release.set()
            if watcher is not None and not watcher.done(): watcher.cancel()
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            if watcher is not None: await asyncio.gather(watcher, return_exceptions=True)
            await asyncio.wait_for(client.close(), 2)
    asyncio.run(scenario())


@pytest.mark.parametrize('path', ['send', 'native-enter', 'native-exit', 'close'])
@pytest.mark.parametrize('fault', ['close', 'reset'])
def test_review_round4_pending_native_waiter(path, fault, monkeypatch):
    from contextlib import asynccontextmanager
    from key_hygiene_fixture import Socket, REQUEST
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close, Frame, OP_PONG
    async def scenario():
        loop = asyncio.get_running_loop()
        native_waiter = loop.create_future()
        entered = asyncio.Event()
        armed = False
        async def native_wait():
            entered.set()
            return await native_waiter
        class Peer(Socket):
            async def send(self, raw):
                if armed and path == 'send' and json.loads(raw)['type'] == 'append_text':
                    await native_wait()
                return await super().send(raw)
            @asynccontextmanager
            async def send_context(self):
                if armed and path == 'native-enter': await native_wait()
                yield
                if armed and path == 'native-exit': await native_wait()
            async def close(self, code=1000):
                if armed and path == 'close': await native_wait()
                return await super().close(code)
        socket_ = Peer()
        class Protocol:
            def send_frame(self, frame): pass
        socket_.protocol = Protocol()
        async def connect(_url, **_options): return socket_
        monkeypatch.setattr(stream_module, 'connect', connect)
        client = stream_module.SystemZeroStreamClient(KEY, max_reconnect_attempts=1, reconnect_policy='never')
        caller = None
        observed = loop.create_future()
        snapshots = []
        try:
            await client.connect()
            await client.start(REQUEST)
            armed = True
            if path == 'send':
                caller = asyncio.create_task(client.append_text('safe'))
            elif path == 'close':
                caller = asyncio.create_task(client.close())
            else:
                socket_.protocol.send_frame(Frame(OP_PONG, b'safe'))
            await asyncio.wait_for(entered.wait(), 2)
            owner = (client._drainer if path == 'send' else client._runner if path == 'close'
                     else next(iter(client._native_tasks)))
            assert not owner.done() and not native_waiter.done()
            def observe():
                retained = [v for v in vars(client).values() if isinstance(v, asyncio.Future)]
                retained.extend(client._native_tasks)
                snapshots.extend([repr(vars(client)), *(repr(v) for v in retained)])
                observed.set_result((not owner.done(), native_waiter.done(), native_waiter.exception() is not None))
            def transport_failure_callback():
                loop.call_soon(observe)
                error = (ConnectionClosedError(Close(1011, KEY), Close(1011, KEY), True)
                         if fault == 'close' else ConnectionResetError(KEY))
                native_waiter.set_exception(error)
            loop.call_soon(transport_failure_callback)
            assert await asyncio.wait_for(observed, 2) == (True, True, True), 'observer must precede pending owner resumption'
            assert all(KEY not in value for value in snapshots), 'pending retained Task/native waiter diagnostics contain the credential'
        finally:
            armed = False
            if not native_waiter.done(): native_waiter.cancel()
            if caller is not None:
                caller.cancel()
                await asyncio.gather(caller, return_exceptions=True)
            await asyncio.wait_for(client.close(), 2)
    asyncio.run(scenario())


@pytest.mark.parametrize('path', [
    'send', 'decide', 'replay', 'receive', 'native-enter', 'native-exit',
    'json-pong', 'probe-send', 'probe-pong', 'close', 'handover-close',
])
@pytest.mark.parametrize('fault', ['reset', 'close'])
def test_review_round5_all_pending_tasks(path, fault, monkeypatch):
    from contextlib import asynccontextmanager
    from key_hygiene_fixture import Socket, REQUEST
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close, Frame, OP_PONG
    async def scenario():
        loop = asyncio.get_running_loop()
        native_waiter = loop.create_future()
        entered, armed = asyncio.Event(), asyncio.Event()
        wait_owners = []
        async def native_wait():
            wait_owners.append(asyncio.current_task())
            entered.set()
            return await native_waiter
        class Peer(Socket):
            async def send(self, raw):
                kind = json.loads(raw)['type']
                if armed.is_set() and ((path == 'send' and kind == 'append_text') or
                        (path == 'decide' and kind == 'decide') or
                        (path == 'replay' and kind == 'start') or
                        (path == 'json-pong' and kind == 'pong')):
                    await native_wait()
                return await super().send(raw)
            async def recv(self):
                if armed.is_set() and path == 'receive': return await native_wait()
                return await super().recv()
            @asynccontextmanager
            async def send_context(self):
                if armed.is_set() and path == 'native-enter': await native_wait()
                yield
                if armed.is_set() and path == 'native-exit': await native_wait()
            async def ping(self):
                await armed.wait()
                if path == 'probe-send': await native_wait()
                if path == 'probe-pong':
                    entered.set()
                    return native_waiter
                return await super().ping()
            async def close(self, code=1000):
                if armed.is_set() and (path == 'close' or path == 'handover-close' and code == 1001):
                    await native_wait()
                return await super().close(code)
        native = path in ('probe-send', 'probe-pong')
        socket_ = Peer(native=native)
        replacement = Peer(identity=2)
        if native:
            hello = json.loads(socket_.incoming.get_nowait())
            hello['limits']['native_heartbeat_interval_ms'] = 1
            socket_.incoming.put_nowait(json.dumps(hello))
        class Protocol:
            def send_frame(self, frame): pass
        socket_.protocol = Protocol()
        opens = []
        async def connect(_url, **_options):
            opens.append(True)
            return socket_ if len(opens) == 1 else replacement
        monkeypatch.setattr(stream_module, 'connect', connect)
        client = stream_module.SystemZeroStreamClient(KEY, max_reconnect_attempts=1, random=lambda: 0)
        caller = None
        observed = loop.create_future()
        snapshots, task_names = [], set()
        try:
            await client.connect()
            await client.start(REQUEST)
            armed.set()
            if path == 'handover-close':
                # Seed the already-tested refused-replay policy branch; the
                # public append still creates the real drainer/transport wait.
                client._replay_on_request = True
            if path in ('send', 'handover-close'):
                caller = asyncio.create_task(client.append_text('safe'))
            elif path == 'decide': caller = asyncio.create_task(client.decide())
            elif path == 'replay': socket_.peer_close(1001)
            elif path == 'close': caller = asyncio.create_task(client.close())
            elif path in ('receive', 'json-pong'): socket_.server('ping', nonce=1)
            elif path in ('native-enter', 'native-exit'):
                socket_.protocol.send_frame(Frame(OP_PONG, b'safe'))
            await asyncio.wait_for(entered.wait(), 2)
            # A returned pong Future must have reached its await, not just ping().
            for _ in range(12): await asyncio.sleep(0)
            assert not native_waiter.done()
            before = asyncio.all_tasks()
            target = (wait_owners[0] if wait_owners else next(task for task in before
                      if task.get_coro().__qualname__.endswith('._native_heartbeat')))
            assert target in before and not target.done() and native_waiter._callbacks
            def observe():
                pending = asyncio.all_tasks()
                snapshots.append(repr(vars(client)))
                snapshots.extend(repr(task) for task in pending)
                task_names.update(task.get_coro().__qualname__ for task in pending)
                observed.set_result((target in pending and not target.done(), native_waiter.done(), native_waiter.exception() is not None))
            def transport_failure_callback():
                loop.call_soon(observe)
                error = (ConnectionResetError(KEY) if fault == 'reset' else
                         ConnectionClosedError(Close(1011, KEY), Close(1011, KEY), True))
                native_waiter.set_exception(error)
            loop.call_soon(transport_failure_callback)
            assert await asyncio.wait_for(observed, 2) == (True, True, True), 'sample pending owners after native failure publication'
            for _ in range(32):
                snapshots.append(repr(vars(client)))
                snapshots.extend(repr(task) for task in asyncio.all_tasks())
                await asyncio.sleep(0)
            assert any('produce' in name for name in task_names), 'enumerate SDK producers, not only client attributes'
            assert all(KEY not in value for value in snapshots), 'a pending SDK Task exposes a native credential-bearing waiter'
        finally:
            armed.clear()
            if not native_waiter.done(): native_waiter.cancel()
            if caller is not None:
                caller.cancel()
                await asyncio.gather(caller, return_exceptions=True)
            await asyncio.wait_for(client.close(), 2)
    asyncio.run(scenario())
