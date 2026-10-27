import asyncio
import json
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close
from websockets.protocol import State
from system_zero import stream as stream_module
from stream_limit_fixture import TEST_STREAM_LIMITS

REQUEST = {
    "model": "system-zero", "state": "SYNTHETIC parity state",
    "questions": {"ready": {"type": "boolean", "instructions": "Ready?"}},
}


def disconnected(code):
    if code == 1006:
        return ConnectionClosedError(None, None)
    kind = ConnectionClosedOK if code in (1000, 1001) else ConnectionClosedError
    return kind(Close(code, "SYNTHETIC peer close"), Close(code, "SYNTHETIC peer close"), True)


class Socket:
    subprotocol = stream_module.STREAM_PROTOCOL

    def __init__(self, identity=1, *, native=False):
        self.stream_id = "szws_" + format(identity, "032x")
        self.incoming = asyncio.Queue()
        self.seq = 0
        self.version = 0
        self.state = State.OPEN
        self.close_code = None
        self.close_calls = []
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_release.set()
        self.active_receives = 0
        self.send_attempts = 0
        self.sent = []
        self.pongs = []
        self.mode = "on_change"
        limits = {**TEST_STREAM_LIMITS, "heartbeat_interval_ms": 30_000}
        if native:
            limits["native_heartbeat_interval_ms"] = 10_000
        self.server("hello", protocol=self.subprotocol, request_id="req_parity_hello",
                    limits=limits, rate_limit=None)

    def server(self, kind, **fields):
        self.seq += 1
        frame = {"type": kind, "stream_id": self.stream_id, "seq": self.seq, **fields}
        self.incoming.put_nowait(json.dumps(frame))
        return frame

    def notice(self, *, fatal=False):
        return self.server(
            "error", phase="lifecycle", http_status=401 if fatal else 503,
            fatal=fatal, state_version=self.version, event_id=None, covered_event_ids=[],
            rate_limit=None, server_timing=None,
            envelope={"request_id": "req_parity_auth" if fatal else "req_parity_notice", "error": {
                "code": "invalid_api_key" if fatal else "service_unavailable",
                "type": "authentication_error" if fatal else "server_error", "param": None,
                "message": "SYNTHETIC refusal" if fatal else "SYNTHETIC notice",
                "retryable": not fatal,
            }},
            retry={"should_retry": not fatal, "retry_after_ms": None, "retry_after": None},
        )

    def goodbye(self):
        return self.server(
            "goodbye", reason="policy", state_version=self.version,
            last_decision_state_version=0,
            resume={"supported": False, "action": "reconnect_and_replay", "retry_after_ms": 0},
        )

    def decision(self):
        return self.server(
            "decision", state_version=self.version, http_status=200,
            reply={"id": "szd_" + "2" * 32, "answers": {"ready": {"decision": True}}},
            covered_event_ids=["evt_%d" % self.version], request_id="req_parity_decision",
            server_timing=None, latency_ms=1, rate_limit=None, queue_ms=0, decide_ms=1,
        )

    def peer_close(self, code=1008):
        self.close_code = code
        self.state = State.CLOSED
        self.incoming.put_nowait(disconnected(code))

    async def recv(self):
        self.active_receives += 1
        try:
            value = await self.incoming.get()
            if isinstance(value, BaseException):
                raise value
            return value
        finally:
            self.active_receives -= 1

    async def send(self, raw):
        self.send_attempts += 1
        if self.state != State.OPEN:
            raise disconnected(self.close_code or 1006)
        frame = json.loads(raw)
        self.sent.append(frame)
        if frame["type"] in {"start", "append_text", "append_items", "replace", "append_attachments"}:
            self.version += 1
            if frame["type"] == "start":
                self.mode = frame["decision_mode"]
            self.server("ack", event_id=frame["event_id"], state_version=self.version,
                        state_bytes=32, synthesized_body_bytes=192, scheduled=self.mode == "on_change")
        elif frame["type"] == "decide":
            self.server("scheduled", state_version=self.version, in_flight=False)
        else:
            assert frame["type"] == "pong", "Unexpected synthetic client frame"

    async def ping(self):
        waiter = asyncio.get_running_loop().create_future()
        self.pongs.append(waiter)
        return waiter

    async def close(self, code=1000):
        self.close_calls.append(code)
        self.close_started.set()
        await self.close_release.wait()
        # A transport close is an asynchronous boundary, even with no external I/O.
        await asyncio.sleep(0)
        if self.close_code is None:
            self.close_code = code
        self.state = State.CLOSED
