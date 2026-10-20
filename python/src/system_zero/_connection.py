"""Transport classification without exposing native messages, URLs, or headers."""
import asyncio
import errno
import re
import socket
import ssl
from typing import Any, Iterator, Optional
import httpcore
import httpx
from .errors import APIConnectionError, APITimeoutError
_NATIVE_CODE = re.compile('[A-Z][A-Z0-9_]{0,63}\\Z')
_WINDOWS_CODES = {10013: 'EACCES', 10022: 'EINVAL', 10035: 'EWOULDBLOCK', 10048: 'EADDRINUSE', 10049: 'EADDRNOTAVAIL', 10050: 'ENETDOWN', 10051: 'ENETUNREACH', 10052: 'ENETRESET', 10053: 'ECONNABORTED', 10054: 'ECONNRESET', 10057: 'ENOTCONN', 10058: 'ESHUTDOWN', 10060: 'ETIMEDOUT', 10061: 'ECONNREFUSED', 10064: 'EHOSTDOWN', 10065: 'EHOSTUNREACH'}

def _causes(error: BaseException, context_boundary: Optional[BaseException]=None) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: Optional[BaseException] = error
    while current is not None and id(current) not in seen and (len(seen) < 16):
        seen.add(id(current))
        yield current
        following = current.__cause__
        if following is None:
            following = current.__context__
            if following is context_boundary:
                break
        current = following

def _is_network(error: BaseException) -> bool:
    if type(error) is APIConnectionError or isinstance(error, APITimeoutError):
        return True
    return isinstance(error, (OSError, EOFError, asyncio.TimeoutError, httpx.TransportError, httpcore.NetworkError, httpcore.TimeoutException, httpcore.ProtocolError, httpcore.ProxyError))

def is_transport_error(error: BaseException, *, context_boundary: Optional[BaseException]=None) -> bool:
    return any((_is_network(cause) for cause in _causes(error, context_boundary)))

class ResponseCloseGuard(httpx.SyncByteStream):
    """Defer a close transport fault until the completed body has been interpreted."""

    def __init__(self, stream: httpx.SyncByteStream, *, context_boundary: Optional[BaseException]=None):
        self._stream = stream
        self._context_boundary = context_boundary
        self.error: Optional[Exception] = None

    def __iter__(self) -> Iterator[bytes]:
        yield from self._stream

    def close(self) -> None:
        try:
            self._stream.close()
        except Exception as error:
            if not is_transport_error(error, context_boundary=self._context_boundary):
                raise
            self.error = self.error or error

def _native_code(error: BaseException, context_boundary: Optional[BaseException]) -> Optional[str]:
    for cause in _causes(error, context_boundary):
        if isinstance(cause, APIConnectionError):
            if cause.transport_code is not None:
                return cause.transport_code
            continue
        if isinstance(cause, ssl.SSLError):
            reason = getattr(cause, 'reason', None)
            if isinstance(reason, str) and _NATIVE_CODE.fullmatch(reason):
                return reason
            continue
        if isinstance(cause, socket.gaierror):
            for name in ('EAI_AGAIN', 'EAI_BADFLAGS', 'EAI_FAIL', 'EAI_FAMILY', 'EAI_MEMORY', 'EAI_NONAME', 'EAI_SERVICE', 'EAI_SOCKTYPE', 'EAI_SYSTEM', 'EAI_OVERFLOW'):
                if cause.errno is not None and cause.errno == getattr(socket, name, None):
                    return name
        code = getattr(cause, 'code', None)
        if isinstance(code, str) and _NATIVE_CODE.fullmatch(code):
            return code
        number = getattr(cause, 'winerror', None) or getattr(cause, 'errno', None)
        if isinstance(number, int):
            name = _WINDOWS_CODES.get(number) or errno.errorcode.get(number)
            if name is not None:
                return name
    return None

def _before_send(error: BaseException, *, context_boundary: Optional[BaseException]) -> bool:
    for cause in _causes(error, context_boundary):
        if isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpcore.ConnectError, httpcore.ConnectTimeout, httpcore.PoolTimeout)):
            return True
    return False

def connection_error(error: BaseException, message: str, *, request_sent: Optional[bool]=None, context_boundary: Optional[BaseException]=None, **fields: Any) -> APIConnectionError:
    if type(error) is APIConnectionError:
        if request_sent is True:
            error.request_sent = True
        return error
    if isinstance(error, APIConnectionError):
        retained = {'status': error.status, 'type': error.type, 'code': error.code, 'param': error.param, 'retryable': error.retryable, 'details': error.details, 'request_id': error.request_id, 'headers': error.headers}
        if isinstance(error, APITimeoutError):
            retained['type'] = 'timeout_error'
            retained['code'] = error.code or 'system_zero_timeout'
        retained.update(fields)
        fields = retained
        if request_sent is None:
            request_sent = error.request_sent
    timeout = any((isinstance(cause, (TimeoutError, asyncio.TimeoutError, APITimeoutError, httpx.TimeoutException, httpcore.TimeoutException)) for cause in _causes(error, context_boundary)))
    if request_sent is None and _before_send(error, context_boundary=context_boundary):
        request_sent = False
    fields.setdefault('type', 'timeout_error' if timeout else 'connection_error')
    if timeout:
        fields.setdefault('code', 'system_zero_timeout')
    return APIConnectionError(message, transport_code=_native_code(error, context_boundary), request_sent=request_sent, **fields)
