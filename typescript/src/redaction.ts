/** Diagnostic redaction, scoped to the client that owns the operation. */
import { debuglog } from 'node:util';

/** Node core tracing has no per-request redaction hook. Fail before handing it headers. */
export function assertSafeNativeDebug(): void {
  if (['http', 'https', 'net', 'tls', 'stream', 'websocket'].some(name => debuglog(name).enabled)) {
    throw new Error('Native HTTP debug logging cannot safely carry credentials. Disable NODE_DEBUG for http, https, net, tls, stream and websocket.');
  }
}

export function redactText(value: string, apiKey?: string): string {
  if (apiKey) value = value.split(apiKey).join('[redacted]');
  return value.replace(/\bBearer[ \t]+[A-Za-z0-9._~+/-]+=*/giu, 'Bearer [redacted]');
}

// Only SDK and standard error prototypes may provide diagnostic behavior.
const errorPrototypes = new WeakSet<object>([Error, TypeError, RangeError, SyntaxError,
  ReferenceError, URIError, EvalError, AggregateError].map(kind => kind.prototype));
export function registerErrorPrototype(prototype: object): void { errorPrototypes.add(prototype); }

const sensitive = /^(authorization|proxy-authorization|api[_-]?key)$/iu;

/** Copy only when redaction is necessary; never mutate a caller's native error. */
export function redact<T>(input: T, apiKey?: string): T {
  const seen = new WeakMap<object, unknown>();
  let remaining = 4096;
  const visit = (value: unknown, depth: number): unknown => {
    if (typeof value === 'string') return redactText(value, apiKey);
    if (value === null || typeof value !== 'object') return value;
    if (depth > 32 || --remaining < 0) return '[omitted]';
    if (seen.has(value)) return seen.get(value);
    if (value instanceof Headers) {
      const copy = new Headers();
      let changed = false;
      for (const [name, item] of value) {
        if (sensitive.test(name)) { changed = true; continue; }
        const safeName = redactText(name, apiKey), safeItem = redactText(item, apiKey);
        // A replacement marker is not a valid HTTP field-name token. Omit it;
        // never let diagnostic formatting replace the classified HTTP failure.
        if (safeName !== name) { changed = true; continue; }
        changed ||= safeItem !== item;
        copy.append(name, safeItem);
      }
      return changed ? copy : value;
    }
    // Maps, URLs, buffers and other opaque objects can hide credentials in
    // internal slots that Reflect.ownKeys cannot inspect safely.
    const prototype = Object.getPrototypeOf(value);
    if (!(value instanceof Error) && !Array.isArray(value) && prototype !== Object.prototype && prototype !== null) return '[omitted]';
    const dom = value instanceof DOMException;
    const error = value instanceof Error;
    let safePrototype = prototype;
    if (error && !dom) {
      while (safePrototype && !errorPrototypes.has(safePrototype)) safePrototype = Object.getPrototypeOf(safePrototype);
      safePrototype ??= Error.prototype;
    }
    const display = (field: 'name' | 'message'): string => {
      const own = Object.getOwnPropertyDescriptor(value, field);
      // Do not invoke foreign inherited accessors or conversion hooks.
      const raw = own && 'value' in own && typeof own.value === 'string' ? own.value
        : dom ? value[field] : field === 'name' ? safePrototype.name : '';
      return redactText(typeof raw === 'string' ? raw : '', apiKey);
    };
    const copy = dom ? new DOMException(display('message'), display('name'))
      : error ? Object.setPrototypeOf(new Error(), safePrototype)
      : Array.isArray(value) ? [] : {};
    seen.set(value, copy);
    let changed = dom ? copy.message !== value.message || copy.name !== value.name || prototype !== DOMException.prototype
      : error && safePrototype !== prototype;
    if (error) {
      for (const field of ['name', 'message'] as const) {
        const safe = display(field), own = Object.getOwnPropertyDescriptor(value, field);
        changed ||= !!own && (!('value' in own) || own.value !== safe);
        Object.defineProperty(copy, field, { value: safe, writable: true, configurable: true });
      }
    }
    for (const key of Reflect.ownKeys(value)) {
      if (Array.isArray(value) && key === 'length') continue;
      if (error && (key === 'name' || key === 'message')) continue;
      if (error && (key === 'toString' || key === 'toJSON')) { changed = true; continue; }
      let descriptor = Object.getOwnPropertyDescriptor(value, key)!;
      // V8 represents Error.stack as a lazy accessor on supported Node versions.
      // Materialize its diagnostic text without treating every benign error as dirty.
      if (key === 'stack' && value instanceof Error && !('value' in descriptor)) {
        let stack: unknown;
        try { stack = value.stack; } catch { stack = '[omitted]'; }
        descriptor = { value: stack, enumerable: descriptor.enumerable ?? false, writable: true, configurable: true };
      }
      if (typeof key === 'symbol' || sensitive.test(key) || !('value' in descriptor) || typeof descriptor.value === 'function') {
        changed = true; continue;
      }
      // Native HTTP objects own sockets, requests and transport configuration.
      const item = /^(request|response|config)$/iu.test(key) ? '[omitted]' : visit(descriptor.value, depth + 1);
      const name = redactText(key, apiKey);
      changed ||= item !== descriptor.value || name !== key;
      Object.defineProperty(copy, name, { ...descriptor, value: item, configurable: true });
    }
    if (!changed) { seen.set(value, value); return value; }
    return copy;
  };
  return visit(input, 0) as T;
}
