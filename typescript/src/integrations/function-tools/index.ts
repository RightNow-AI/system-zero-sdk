import { createHash } from 'node:crypto';
import { checkedBaseURL, redactClientText, withRetryContext } from '../../client.js';
import { SystemZeroClient } from '../../client.js';
import { InvalidRequestError, SystemZeroError } from '../../errors.js';
import type { DecideRequest, Question, StructuredJson } from '../../api-types.js';
import { SYSTEM_ZERO_ERRORS as API_ERRORS } from '../error-catalog.js';
import sourceDefinitions from './tool-schemas.js';
import argumentLimits from './argument-limits.js';
import gatewayDefinitions from './gateway-errors.js';

function gatewayErrorCatalog<T extends Record<string, { status: number; message: string }>>(input: T) {
  const catalog = {} as { [Code in keyof T]: readonly [number, string] };
  for (const code in input) {
    if (!Object.hasOwn(input, code)) continue;
    const value = input[code]!;
    catalog[code] = [value.status, value.message];
  }
  return catalog;
}
const SYSTEM_ZERO_ERRORS = { ...API_ERRORS, ...gatewayErrorCatalog(gatewayDefinitions) };
type SystemZeroErrorCode = keyof typeof SYSTEM_ZERO_ERRORS;

const definitions = structuredClone(sourceDefinitions);
// Publication follows OpenAPI; retained raw argument acceptance is unchanged.
const acceptedDefinitions = structuredClone(sourceDefinitions);
type Schema = { type?: string | string[]; enum?: unknown[]; properties?: Record<string, Schema | undefined>; required?: string[]; additionalProperties?: boolean; items?: Schema; anyOf?: Schema[] };
export interface ToolResult {
  status: 'ok' | 'held' | 'error';
  request_id: string | null;
  error?: { code: SystemZeroErrorCode | 'call_limit_reached'; message: string };
  [key: string]: unknown;
}
export interface ToolHandlerOptions {
  client?: Pick<SystemZeroClient, 'decide' | 'models'>;
  baseURL?: string;
  namespace?: string;
  maxDecideCalls?: number;
}
export interface ToolHandler {
  (name: string, args: unknown): Promise<ToolResult>;
  readonly budget: Readonly<{ decideCalls: number }>;
}

export const NESTED_FUNCTION_TOOLS = structuredClone(definitions).map(({ name, description, inputSchema }) => ({ type: 'function' as const, function: { name, description, parameters: inputSchema, strict: true as const } }));
export const FLAT_FUNCTION_TOOLS = structuredClone(NESTED_FUNCTION_TOOLS).map(({ function: tool }) => ({ type: 'function' as const, ...tool }));
export const INPUT_SCHEMA_TOOLS = structuredClone(definitions).map(({ name, description, inputSchema }) => ({ name, description, input_schema: inputSchema }));
export const TOOL_DEFINITIONS = structuredClone(definitions).map(tool => {
  tool.inputSchema.required = tool.name === 'system_zero_decide' ? ['state', 'questions'] : [];
  const question = (tool.inputSchema as Schema).properties?.questions?.items;
  for (const variant of question?.anyOf ?? []) {
    if (variant.properties?.type?.enum?.includes('boolean')) variant.required = variant.required?.filter(field => field !== 'criteria');
  }
  return tool;
});

export class ToolArgumentError extends InvalidRequestError {
  constructor() { super(SYSTEM_ZERO_ERRORS.invalid_request[1], { code: 'invalid_request', status: 400, type: 'invalid_request_error' }); }
}
function invalid(): never { throw new ToolArgumentError(); }
function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value) && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
}
function validString(value: string): boolean {
  // UTF-8 cannot faithfully carry an unpaired UTF-16 surrogate.
  for (let i = 0; i < value.length; i++) {
    const unit = value.charCodeAt(i);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = value.charCodeAt(++i);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return false;
    } else if (unit >= 0xdc00 && unit <= 0xdfff) return false;
  }
  return true;
}

/** Screen raw JSON before allocating its parsed tree, including discarded duplicate names. */
function screenJsonText(text: string, values = 0): number {
  if (text.length > argumentLimits.maxBytes || Buffer.byteLength(text, 'utf8') > argumentLimits.maxBytes) invalid();
  let depth = 0;
  for (let i = 0; i < text.length;) {
    const unit = text[i++]!;
    if (unit === ' ' || unit === '\t' || unit === '\r' || unit === '\n' || unit === ':' || unit === ',') continue;
    if (unit === '}' || unit === ']') { if (--depth < 0) invalid(); continue; }
    if (++values > argumentLimits.maxValues) invalid();
    if (unit === '{' || unit === '[') {
      if (++depth > argumentLimits.maxDepth) invalid();
    } else if (unit === '"') {
      let closed = false;
      while (i < text.length) {
        const character = text[i++]!;
        if (character === '\\') i++;
        else if (character === '"') { closed = true; break; }
      }
      if (!closed) invalid();
    } else {
      // Syntax and the finite number domain remain the parser's responsibility.
      while (i < text.length && !'{}[],: \t\r\n'.includes(text[i]!)) i++;
    }
  }
  if (depth !== 0) invalid();
  return values;
}

/** Own no copy until the whole outer object and its encoded documents are bounded. */
function screenArguments(value: unknown, rawValues?: number): void {
  let bytes = 0;
  let values = rawValues ?? 0;
  const active = new Set<object>();
  const addBytes = (amount: number): void => { if ((bytes += amount) > argumentLimits.maxBytes) invalid(); };
  // Raw input has already charged every outer token, including duplicate names.
  const addValue = (): void => { if (rawValues === undefined && ++values > argumentLimits.maxValues) invalid(); };
  const string = (text: string): void => {
    if (text.length > argumentLimits.maxBytes - bytes) invalid();
    addBytes(2);
    for (let i = 0; i < text.length; i++) {
      const unit = text.charCodeAt(i);
      if (unit === 34 || unit === 92 || [8, 9, 10, 12, 13].includes(unit)) addBytes(2);
      else if (unit < 32) addBytes(6);
      else if (unit < 128) addBytes(1);
      else if (unit < 2048) addBytes(2);
      else if (unit >= 0xd800 && unit <= 0xdbff) {
        const next = text.charCodeAt(++i);
        if (!(next >= 0xdc00 && next <= 0xdfff)) invalid();
        addBytes(4);
      } else if (unit >= 0xdc00 && unit <= 0xdfff) invalid();
      else addBytes(3);
    }
  };
  const visit = (item: unknown, depth: number): void => {
    addValue();
    if (item === null) { addBytes(4); return; }
    if (typeof item === 'boolean') { addBytes(item ? 4 : 5); return; }
    if (typeof item === 'string') { string(item); return; }
    if (typeof item === 'number') {
      if (!Number.isFinite(item)) invalid();
      // A shared conservative allowance avoids language-specific float formatting.
      addBytes(25); return;
    }
    if ((!Array.isArray(item) && !record(item)) || active.has(item)) invalid();
    // Snapshotting must not invoke an own or inherited serialization hook.
    if ('toJSON' in item) invalid();
    if (++depth > argumentLimits.maxDepth) invalid();
    active.add(item);
    addBytes(2);
    if (Array.isArray(item)) {
      if (rawValues === undefined && item.length > argumentLimits.maxValues - values) invalid();
      for (let index = 0; index < item.length; index++) {
        if (index) addBytes(1);
        const property = Object.getOwnPropertyDescriptor(item, String(index));
        if (!property || !Object.hasOwn(property, 'value')) invalid();
        visit(property.value, depth);
      }
    } else {
      let first = true;
      for (const key in item) {
        if (!Object.hasOwn(item, key)) continue;
        const property = Object.getOwnPropertyDescriptor(item, key);
        if (!property || !Object.hasOwn(property, 'value')) invalid();
        if (!first) addBytes(1);
        first = false;
        addValue(); string(key); addBytes(1);
        visit(property.value, depth);
        if (key === 'json' && typeof property.value === 'string') values = screenJsonText(property.value, values);
      }
    }
    active.delete(item);
  };
  visit(value, 0);
}

function jsonValue(value: unknown, seen = new Set<object>()): boolean {
  if (value === null || typeof value === 'boolean') return true;
  if (typeof value === 'string') return validString(value);
  if (typeof value === 'number') return Number.isFinite(value);
  if (!Array.isArray(value) && !record(value)) return false;
  if (seen.has(value)) return false;
  seen.add(value);
  const valid = (Array.isArray(value) ? value : Object.entries(value).flat()).every(item => jsonValue(item, seen));
  seen.delete(value);
  return valid;
}
function matches(schema: Schema, value: unknown): boolean {
  if (schema.anyOf) return schema.anyOf.some(branch => matches(branch, value));
  if (schema.enum && !schema.enum.includes(value)) return false;
  const types = Array.isArray(schema.type) ? schema.type : [schema.type];
  const type = value === null ? 'null' : Array.isArray(value) ? 'array' : typeof value;
  if (!types.includes(type)) return false;
  if (type === 'object') {
    if (!record(value)) return false;
    const properties = schema.properties ?? {};
    if (Object.keys(value).some(key => !Object.hasOwn(properties, key))) return false;
    if ((schema.required ?? []).some(key => !Object.hasOwn(value, key))) return false;
    return Object.entries(value).every(([key, item]) => matches(properties[key]!, item));
  }
  return type !== 'array' || (value as unknown[]).every(item => matches(schema.items!, item));
}
function structured(value: unknown): StructuredJson {
  if (typeof value === 'string') return value;
  if (!record(value) || typeof value.json !== 'string') invalid();
  const parsed: unknown = JSON.parse(value.json);
  if ((!record(parsed) && !Array.isArray(parsed)) || !jsonValue(parsed)) invalid();
  return parsed as StructuredJson;
}

/** Bound provider volume, then validate shape; decide owns its request caps and availability. */
export function validateToolArguments(name: string, args: unknown): DecideRequest | Record<string, never> {
  try {
    const rawValues = typeof args === 'string' ? screenJsonText(args) : undefined;
    let value: unknown = typeof args === 'string' ? JSON.parse(args) : args;
    if (!record(value)) invalid();
    screenArguments(value, rawValues);
    const definition = acceptedDefinitions.find(tool => tool.name === name);
    if (!definition) invalid();
    if (name === 'system_zero_list_models') {
      if (!matches(definition.inputSchema as Schema, value)) invalid();
      return {};
    }
    // Own a snapshot before any asynchronous work; null optionals are omitted on wire.
    value = JSON.parse(JSON.stringify({ model: null, attachments: null, ...value }));
    const input = value as Record<string, unknown>;
    if (Array.isArray(input.questions)) input.questions = input.questions.map(question => record(question) && question.type === 'boolean' ? { criteria: null, ...question } : question);
    if (!matches(definition.inputSchema as Schema, input)) invalid();
    const entries: [string, Question][] = [];
    const names = new Set<string>();
    for (const item of input.questions as Record<string, unknown>[]) {
      const name = item.name as string;
      if (names.has(name)) invalid();
      names.add(name);
      const instructions = structured(item.instructions);
      let question: Question;
      if (item.type === 'boolean') {
        const criteria = item.criteria === null ? undefined : Object.fromEntries(Object.entries(item.criteria as Record<string, unknown>).filter(([, value]) => value !== null));
        question = { type: 'boolean', instructions, ...(criteria ? { criteria } : {}) };
      } else if (item.type === 'choice') {
        const choices = item.options as { label: string; description: string | null }[];
        if (new Set(choices.map(choice => choice.label)).size !== choices.length) invalid();
        question = { type: 'choice', instructions, criteria: Object.fromEntries(choices.map(choice => [choice.label, choice.description])) };
      } else question = { type: 'score', instructions, criteria: item.options as string[] };
      entries.push([name, question]);
    }
    return { model: input.model === null ? 'system-zero' : input.model as string, state: structured(input.state), questions: Object.fromEntries(entries), ...(input.attachments === null ? {} : { attachments: input.attachments as DecideRequest['attachments'] }) };
  } catch { invalid(); }
}

/** Shared binary-number canonical form avoids language-specific JSON float formatting. */
function canonical(value: unknown): string {
  if (value === null) return 'n';
  if (typeof value === 'boolean') return value ? 'b1' : 'b0';
  if (typeof value === 'number') {
    const bytes = Buffer.alloc(8); bytes.writeDoubleBE(value === 0 ? 0 : value);
    return `d${bytes.toString('hex')}`;
  }
  if (typeof value === 'string') return `s${Buffer.byteLength(value)}:${value}`;
  if (Array.isArray(value)) return `a${value.length}:[${value.map(canonical).join('')}]`;
  const object = value as Record<string, unknown>;
  const keys = Object.keys(object).sort((a, b) => Buffer.compare(Buffer.from(a), Buffer.from(b)));
  return `o${keys.length}:[${keys.map(key => canonical(key) + canonical(object[key])).join('')}]`;
}
export function deterministicKey(namespace: string, request: unknown): string {
  if (!validString(namespace) || !jsonValue(request)) invalid();
  return 'sz-tools-' + createHash('sha256').update(canonical([namespace, request]), 'utf8').digest('hex');
}
function cleanResult(value: ToolResult, client: ToolHandlerOptions['client']): ToolResult {
  const clean = (item: unknown): unknown => {
    if (typeof item === 'string') return redactClientText(client, item);
    if (Array.isArray(item)) return item.map(clean);
    if (record(item)) return Object.fromEntries(Object.entries(item).map(([key, child]) => [redactClientText(client, key), clean(child)]));
    return item;
  };
  return clean(value) as ToolResult;
}

function refusal(code: SystemZeroErrorCode, requestId: string | null = null, message: string = SYSTEM_ZERO_ERRORS[code][1]): ToolResult {
  return { status: code.endsWith('_not_available') ? 'held' : 'error', request_id: requestId, error: { code, message } };
}

function success(data: Record<string, unknown>, requestId: string | null): ToolResult {
  const answers = record(data.answers) ? data.answers : {};
  const names = Object.keys(answers).sort((a, b) => Buffer.compare(Buffer.from(a), Buffer.from(b)));
  if (names.length && names.every(name => record(answers[name]) && record(answers[name].error))) {
    const codes = names.map(name => {
      const code = ((answers[name] as Record<string, unknown>).error as Record<string, unknown>).code;
      return typeof code === 'string' && Object.hasOwn(SYSTEM_ZERO_ERRORS, code) ? code as SystemZeroErrorCode : 'system_zero_unavailable';
    });
    const code = codes.find(code => !code.endsWith('_not_available')) ?? codes[0]!;
    return { ...data, ...refusal(code, requestId) };
  }
  return { ...data, status: 'ok', request_id: requestId };
}

export function createToolHandler(options: ToolHandlerOptions = {}): ToolHandler {
  const namespace = options.namespace ?? 'system-zero-tools-v1';
  const maxCalls = options.maxDecideCalls ?? 100;
  if (typeof namespace !== 'string' || !namespace || !validString(namespace) || (!Number.isSafeInteger(maxCalls) || maxCalls < 0)) invalid();
  if (options.baseURL !== undefined) checkedBaseURL(options.baseURL);
  let client = options.client;
  let decideCalls = 0;
  const invoke = async (name: string, args: unknown): Promise<ToolResult> => {
    try {
      const request = validateToolArguments(name, args);
      if (name === 'system_zero_list_models') {
        client ??= new SystemZeroClient({ baseURL: options.baseURL });
        const { data, metadata } = await client.models.list(withRetryContext({}, { workflowRecovery: true })).withResponse();
        if (!record(data)) return refusal('system_zero_unavailable');
        return { ...data, status: 'ok', request_id: metadata.requestId };
      }
      if (decideCalls >= maxCalls) return { status: 'error', request_id: null, error: { code: 'call_limit_reached', message: 'The local call limit has been reached.' } };
      const key = deterministicKey(namespace, request);
      client ??= new SystemZeroClient({ baseURL: options.baseURL });
      // Admit synchronously so parallel callers share the count cap.
      decideCalls++;
      const { data, metadata } = await client.decide(request as DecideRequest, withRetryContext({ idempotencyKey: key }, { workflowRecovery: true })).withResponse();
      if (!record(data)) return refusal('system_zero_unavailable');
      return success(data, metadata.requestId || data.id || null);
    } catch (error) {
      if (error instanceof SystemZeroError && error.code && Object.hasOwn(SYSTEM_ZERO_ERRORS, error.code)) return refusal(error.code as SystemZeroErrorCode, error.request_id, error.message);
      return refusal('system_zero_unavailable');
    }
  };
  const handler = async (name: string, args: unknown): Promise<ToolResult> => cleanResult(await invoke(name, args), client);
  Object.defineProperty(handler, 'budget', { get: () => Object.freeze({ decideCalls }) });
  return handler as ToolHandler;
}

/** Configure an owned tool loop with the factory before admitting decide calls. */
export const handleToolCall = createToolHandler({ maxDecideCalls: 0 });
