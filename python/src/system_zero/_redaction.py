"""Credential-safe diagnostics without retaining credentials after client GC."""
import functools
import builtins
import logging
import re
from contextvars import ContextVar
from types import MappingProxyType, MemberDescriptorType, GetSetDescriptorType
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK, ConnectionClosedError
from websockets.frames import Close
from collections.abc import Mapping

_active_key = ContextVar('system_zero_diagnostic_key', default=None)
_sensitive = re.compile(r'^(authorization|proxy-authorization|api[_-]?key)$', re.I)


class Secret:
    __slots__ = ('__value', '__weakref__')

    def __init__(self, value):
        self.__value = value

    def reveal(self):
        return self.__value

    def __repr__(self):
        return '[redacted]'

    __str__ = __repr__

    def __reduce_ex__(self, protocol):
        raise TypeError('SDK credentials cannot be pickled.')


def reveal(value):
    # Revalidation still handles an application overwriting a private attribute.
    return value.reveal() if isinstance(value, Secret) else value


def redact_text(value, key=None):
    key = reveal(key if key is not None else _active_key.get())
    if key:
        value = value.replace(key, '[redacted]')
    return re.sub(r'\bBearer[ \t]+[A-Za-z0-9._~+/-]+=*', 'Bearer [redacted]', value, flags=re.I)


def redact(value, _seen=None, _depth=0):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bytes):
        clean = redact_text(value.decode('utf-8', errors='replace')).encode('utf-8')
        return value if clean == value else clean
    if value is None or isinstance(value, (bool, int, float, Secret)):
        return value
    seen = set() if _seen is None else _seen
    if id(value) in seen or _depth > 32 or len(seen) > 4096:
        return '[omitted]'
    seen.add(id(value))
    if isinstance(value, Mapping):
        clean = {redact_text(str(k)): redact(v, seen, _depth + 1) for k, v in value.items()
                 if not _sensitive.fullmatch(str(k))}
        return value if len(clean) == len(value) and all(k in value and v is value[k] for k, v in clean.items()) else clean
    if isinstance(value, (tuple, list)):
        clean = type(value)(redact(v, seen, _depth + 1) for v in value)
        return value if all(a is b for a, b in zip(value, clean)) else clean
    return '[omitted]'


def protect_exception(error, seen=None):
    """Scrub reachable native causes, including suppressed Python contexts."""
    seen = set() if seen is None else seen
    if id(error) in seen:
        return Exception('Cyclic exception context omitted.')
    if len(seen) >= 32:
        return Exception('Further exception context omitted.')
    seen.add(id(error))
    # Foreign exception classes may print or pickle hidden slots through custom hooks.
    # Keep SDK/builtin classifications; retain only plain diagnostic state otherwise.
    module = type(error).__module__
    close = isinstance(error, ConnectionClosed)
    if close:
        def frame(value):
            return None if value is None else Close(value.code, redact_text(value.reason))
        cls = ConnectionClosedOK if isinstance(error, ConnectionClosedOK) else ConnectionClosedError if isinstance(error, ConnectionClosedError) else ConnectionClosed
        clean = cls(frame(error.rcvd), frame(error.sent), error.rcvd_then_sent)
        clean.__dict__.update({name: value for name, value in vars(error).items()
                               if name not in ('rcvd', 'sent', 'rcvd_then_sent')})
        clean.__cause__, clean.__context__, clean.__traceback__ = error.__cause__, error.__context__, error.__traceback__
        error = clean
    elif module != 'builtins' and not module.startswith('system_zero.'):
        clean = Exception(*redact(error.args))
        clean.__dict__.update(vars(error))
        clean.__cause__, clean.__context__, clean.__traceback__ = error.__cause__, error.__context__, error.__traceback__
        error = clean
    group = isinstance(error, getattr(builtins, 'BaseExceptionGroup', ()))
    if group:
        children = tuple(protect_exception(child, seen) for child in error.exceptions)
        clean = builtins.BaseExceptionGroup(redact_text(error.message), children)
        clean.__dict__.update(vars(error))
        clean.__cause__, clean.__context__, clean.__traceback__ = error.__cause__, error.__context__, error.__traceback__
        error = clean
    elif not close:
        error.args = redact(error.args)
    # Built-in exception printing and pickling use native slots independently
    # of args/__dict__ (SyntaxError, UnicodeError, AttributeError, ImportError...).
    # Enumerate the built-in descriptors, not arbitrary foreign properties.
    excluded = {'args', '__dict__', '__weakref__', '__traceback__', '__cause__', '__context__', 'exceptions', 'message'}
    for cls in type(error).__mro__:
        if cls.__module__ != 'builtins':
            continue
        for name, descriptor in vars(cls).items():
            if name.startswith('__') or name in excluded or not isinstance(descriptor, (MemberDescriptorType, GetSetDescriptorType)):
                continue
            value = getattr(error, name, None)
            clean = redact(value)
            if clean is value:
                continue
            try:
                setattr(error, name, clean)
            except (AttributeError, TypeError):
                # Unknown read-only native state cannot safely retain its type.
                clean = Exception(*redact(error.args))
                clean.__dict__.update(vars(error))
                clean.__cause__, clean.__context__, clean.__traceback__ = error.__cause__, error.__context__, error.__traceback__
                error = clean
                break
    for name, value in list(vars(error).items()):
        if redact_text(name) != name or _sensitive.fullmatch(name) or name in ('request', '_request', 'response', 'config'):
            delattr(error, name)
        elif close and name in ('rcvd', 'sent'):
            continue
        elif isinstance(value, BaseException):
            setattr(error, name, protect_exception(value, seen))
        else:
            clean = redact(value)
            setattr(error, name, MappingProxyType(clean) if isinstance(value, MappingProxyType) else clean)
    for name in ('__cause__', '__context__'):
        cause = getattr(error, name)
        if cause is not None:
            setattr(error, name, protect_exception(cause, seen))
    return error


def protected(method):
    @functools.wraps(method)
    def call(*args, **kwargs):
        owner = args[0] if args else kwargs.get('client')
        token = _active_key.set(getattr(owner, '_api_key', getattr(getattr(owner, '_client', None), '_api_key', None)))
        try:
            try:
                return method(*args, **kwargs)
            except Exception as error:
                failure = protect_exception(error)
                if failure is error:
                    raise
            # Outside the except suite: do not attach the raw group as context.
            raise failure from None
        finally:
            _active_key.reset(token)
    return call


def protected_async(method):
    @functools.wraps(method)
    async def call(*args, **kwargs):
        token = _active_key.set(getattr(args[0], '_api_key', None))
        try:
            try:
                return await method(*args, **kwargs)
            except Exception as error:
                failure = protect_exception(error)
                if failure is error:
                    raise
            raise failure from None
        finally:
            _active_key.reset(token)
    return call


class DiagnosticLogger(logging.LoggerAdapter):
    """Redact before LogRecord creation; leave application logging untouched."""
    def __init__(self, key):
        super().__init__(logging.getLogger('websockets.client'), {})
        self._key = key

    def log(self, level, msg, *args, **kwargs):
        if self.isEnabledFor(level):
            text = str(msg) % args if args else str(msg)
            # Native exceptions may carry requests. Never attach exc_info here.
            kwargs.pop('exc_info', None)
            kwargs.pop('extra', None)
            self.logger.log(level, redact_text(text, self._key), **kwargs)


class _ContextFilter(logging.Filter):
    def filter(self, record):
        if _active_key.get() is not None:
            record.msg = redact_text(record.getMessage())
            record.args = ()
            # A native traceback can retain a request with credential headers.
            record.exc_info = record.exc_text = None
        return True


# Library loggers, not handlers or the root logger. ContextVar isolates other callers.
_filter = _ContextFilter()
for _logger_name in ('httpx', 'httpcore.connection', 'httpcore.http11', 'httpcore.http2', 'httpcore.proxy', 'httpcore.socks'):
    logging.getLogger(_logger_name).addFilter(_filter)
