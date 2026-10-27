import { createHash, type Hash } from 'node:crypto';
import type { Attachment, DecideRequest, JsonValue, Question } from './api-types.js';
import { withRetryContext, redactClientDiagnostics } from './client.js';
import type { SystemZeroClient } from './client.js';
import { decisionsFor, type Alternative, type DecisionResult, type SystemZeroReply } from './decisions.js';
import { SYSTEM_ZERO_ERROR_CODES, GATEWAY_ERROR_CODES } from './public-error-codes.js';
import { APIConnectionError, APIError, APITimeoutError, InvalidRequestError, responseError, SystemZeroError } from './errors.js';

export type FlowCondition = { step: string; question: string } & (
  { op: 'eq'; value: boolean | string } | { op: 'in'; values: Array<boolean | string> }
  | { op: 'confidence_gte' | 'confidence_lt'; value: number }
);
export type FlowTemplate = { literal: JsonValue } | { state: true }
  | { answer: { step: string; question: string; field: 'decision' | 'confidence' | 'alternatives' } }
  | { object: Record<string, FlowTemplate> } | { array: FlowTemplate[] };
export interface FlowStep {
  id: string;
  questions: Record<string, Question>;
  depends_on?: FlowCondition[];
  state_template?: FlowTemplate;
  attachments?: Attachment[] | null;
  next: { rules: Array<{ when: FlowCondition; next: string | null }>; default: string | null };
}
export interface FlowDefinition {
  version: 1;
  request: Omit<DecideRequest, 'questions'>;
  steps: FlowStep[];
}
export interface FlowOptions {
  /** Explicit long-context request option; workspace opt-in is also required. */
  dangerZone?: 'accept';
  run_id: string;
  /** Call count cap, defaulting to MAX_STEPS (32). Larger caps are refused. */
  max_decide_calls?: number;
  /** Duration in milliseconds. The existing client's entire retry bound must fit. */
  deadline_ms: number;
  include_content?: boolean;
  /** Monotonic milliseconds, injectable for deterministic tests. */
  now?: () => number;
}
export interface FlowAnswerTrace {
  question_index: number;
  kind: 'v1' | 'legacy' | 'refusal';
  confidence: number | null;
  question?: string;
  decision?: boolean | string;
  alternatives?: Alternative[];
}
export interface FlowErrorTrace {
  code: string;
  type: string;
  status: number | null;
  retryable: boolean;
}
export interface FlowStepTrace {
  step_index: number;
  status: 'completed' | 'skipped' | 'held' | 'refused' | 'failed';
  request_id: string | null;
  request_hash: string | null;
  idempotency_key: string | null;
  question_count: number;
  started_ms: number;
  elapsed_ms: number;
  /** Number of admitted decide calls, including failed calls. */
  decide_calls: number;
  replayed: boolean;
  usage: Record<string, number> | null;
  answers: FlowAnswerTrace[];
  branch: null | { rule_index: number | null; next_step_index: number | null; reason: 'rule_matched' | 'default' | 'dependency_unmet' };
  error?: FlowErrorTrace;
  step_id?: string;
  request?: DecideRequest;
  questions?: Record<string, Question>;
}
export interface FlowTrace {
  version: 1;
  run_hash: string;
  status: 'completed' | 'refused' | 'held' | 'failed';
  started_ms: number;
  elapsed_ms: number;
  /** Number of admitted decide calls, including failed calls. */
  decide_calls: number;
  steps: FlowStepTrace[];
}
export type FlowError = SystemZeroError & { trace: FlowTrace };

const MAX_STEPS = 32;
const idPattern = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/u;
const knownCodes = new Set<string>([...SYSTEM_ZERO_ERROR_CODES, ...GATEWAY_ERROR_CODES, 'call_limit_reached']);
const heldCodes = new Set([
  'long_context_not_available', 'long_recording_not_available', 'video_not_available', 'image_not_available',
  'audio_not_available', 'document_not_available', 'committee_not_available', 'deliberate_not_available',
]);
const errorTypes = new Set(['invalid_request_error', 'authentication_error', 'permission_error', 'not_found_error',
  'rate_limit_error', 'overloaded_error', 'api_error', 'server_error']);
const usageFields = ['input_tokens', 'cached_input_tokens', 'media_tokens', 'long_input_tokens', 'media_items', 'thought_tokens'];
const questionRefusals: Record<string, string> = {
  injection_suspected: 'The request was refused because its content reads as instructions to the model. Remove the embedded instructions and send it again.',
  question_not_grounded: 'The question cannot be answered from this state.',
};
const heldMessages: Record<string, string> = {
  long_context_not_available: 'This route is not available.', long_recording_not_available: 'This input type is not supported.',
  video_not_available: 'This input type is not supported.',
  image_not_available: 'Images are unavailable. Send the request without the image attachment.',
  audio_not_available: 'Audio is unavailable. Send the request without the audio attachment.',
  document_not_available: 'Documents are unavailable. Send the request without the document attachment.',
  committee_not_available: 'This route is not available.', deliberate_not_available: 'This mode is not available.',
};
function invalid(): InvalidRequestError {
  return new InvalidRequestError('The request is invalid.', { status: 400, type: 'invalid_request_error', code: 'invalid_request', retryable: false });
}
function unavailable(requestId: string | null = null): APIError {
  return new APIError('RightNow cannot serve this request right now. Retry after the Retry-After delay.',
    { status: 503, type: 'api_error', code: 'service_unavailable', retryable: true, request_id: requestId });
}
function timeout(requestId: string | null = null): APITimeoutError {
  return new APITimeoutError('RightNow exceeded the response deadline.',
    { status: 504, type: 'api_error', code: 'system_zero_timeout', retryable: false, request_id: requestId });
}
function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
function exact(value: unknown, required: readonly string[], optional: readonly string[] = []): asserts value is Record<string, unknown> {
  if (!record(value) || required.some(key => !Object.hasOwn(value, key)) || Object.keys(value).some(key => !required.includes(key) && !optional.includes(key))) throw invalid();
}
function validString(value: string): boolean {
  for (const character of value) {
    const unit = character.charCodeAt(0);
    if (character.length === 1 && unit >= 0xd800 && unit <= 0xdfff) return false;
  }
  return true;
}

/** Snapshot plain JSON without executing accessors, toJSON, or caller callbacks. */
function snapshot(value: unknown): JsonValue {
  const active = new Set<object>();
  function copy(item: unknown): JsonValue {
    if (item === null || typeof item === 'boolean') return item;
    if (typeof item === 'string') { if (!validString(item)) throw invalid(); return item; }
    if (typeof item === 'number') {
      if (!Number.isFinite(item) || (Number.isInteger(item) && !Number.isSafeInteger(item))) throw invalid();
      return Object.is(item, -0) ? 0 : item;
    }
    if (typeof item !== 'object' || active.has(item)) throw invalid();
    const array = Array.isArray(item);
    const prototype: unknown = Object.getPrototypeOf(item);
    if (array ? prototype !== Array.prototype : prototype !== Object.prototype && prototype !== null) throw invalid();
    const descriptors = Object.getOwnPropertyDescriptors(item);
    const keys = Reflect.ownKeys(descriptors);
    if (keys.some(key => typeof key !== 'string')) throw invalid();
    active.add(item);
    try {
      if (array) {
        if (keys.length !== item.length + 1) throw invalid();
        const result: JsonValue[] = [];
        for (let index = 0; index < item.length; index++) {
          const descriptor = descriptors[String(index)];
          if (!descriptor || !Object.hasOwn(descriptor, 'value') || !descriptor.enumerable) throw invalid();
          result.push(copy(descriptor.value));
        }
        return result;
      }
      const entries: Array<[string, JsonValue]> = [];
      for (const key of keys as string[]) {
        const descriptor = descriptors[key]!;
        if (!validString(key) || !Object.hasOwn(descriptor, 'value') || !descriptor.enumerable) throw invalid();
        entries.push([key, copy(descriptor.value)]);
      }
      return Object.fromEntries(entries);
    } finally { active.delete(item); }
  }
  try { return copy(value); } catch { throw invalid(); }
}

function hashValue(hash: Hash, value: JsonValue): void {
  if (value === null) { hash.update('n'); return; }
  if (typeof value === 'boolean') { hash.update(value ? 't' : 'f'); return; }
  if (typeof value === 'number') {
    const bytes = Buffer.allocUnsafe(8);
    bytes.writeDoubleBE(value === 0 ? 0 : value);
    hash.update(`d${bytes.toString('hex')}`); return;
  }
  if (typeof value === 'string') {
    const bytes = Buffer.from(value, 'utf8');
    hash.update(`s${bytes.length}:`); hash.update(bytes); return;
  }
  if (Array.isArray(value)) {
    hash.update('['); for (const child of value) hashValue(hash, child); hash.update(']'); return;
  }
  hash.update('{');
  for (const key of Object.keys(value).sort((left, right) => Buffer.compare(Buffer.from(left), Buffer.from(right)))) {
    hashValue(hash, key); hashValue(hash, value[key]!);
  }
  hash.update('}');
}
function digest(value: JsonValue): string {
  const hash = createHash('sha256'); hashValue(hash, value); return hash.digest('hex');
}
/** Hash every exact JSON value; whitespace and object insertion order are immaterial. */
export function flowRequestHash(request: unknown): string { return digest(snapshot(request)); }
function checkRunId(runId: unknown): asserts runId is string {
  if (typeof runId !== 'string' || !validString(runId) || Buffer.byteLength(runId) < 1 || Buffer.byteLength(runId) > 256) throw invalid();
}
export function flowIdempotencyKey(runId: string, stepId: string, request: unknown): string {
  checkRunId(runId);
  if (typeof stepId !== 'string' || !idPattern.test(stepId)) throw invalid();
  return `sz-flow-v1-${digest([runId, stepId, flowRequestHash(request)])}`;
}

/** Validate the complete graph, including unreachable steps, before any call. */
export function validateFlow(input: unknown): FlowDefinition {
  const value: unknown = snapshot(input);
  exact(value, ['version', 'request', 'steps']);
  if (value.version !== 1 || !Array.isArray(value.steps) || value.steps.length < 1 || value.steps.length > MAX_STEPS) throw invalid();
  exact(value.request, ['model', 'state'], ['attachments', 'session_id', 'prompt_cache_key', 'route', 'mode']);
  const indices = new Map<string, number>();
  for (const [index, step] of value.steps.entries()) {
    exact(step, ['id', 'questions', 'next'], ['depends_on', 'state_template', 'attachments']);
    if (typeof step.id !== 'string' || !idPattern.test(step.id) || indices.has(step.id)) throw invalid();
    if (!record(step.questions) || Object.keys(step.questions).length < 1) throw invalid();
    indices.set(step.id, index);
  }
  const definition = value as unknown as FlowDefinition;
  function reference(value: Record<string, unknown>, index: number, current: boolean): void {
    if (typeof value.step !== 'string' || typeof value.question !== 'string') throw invalid();
    const at = indices.get(value.step);
    if (at === undefined || at > index || (!current && at === index) || !Object.hasOwn(definition.steps[at]!.questions, value.question)) throw invalid();
  }
  function condition(value: unknown, index: number, current: boolean): void {
    if (!record(value)) throw invalid();
    if (value.op === 'in') {
      exact(value, ['step', 'question', 'op', 'values']);
      if (!Array.isArray(value.values) || value.values.some(item => typeof item !== 'string' && typeof item !== 'boolean')) throw invalid();
    } else {
      exact(value, ['step', 'question', 'op', 'value']);
      if (value.op === 'eq') { if (typeof value.value !== 'string' && typeof value.value !== 'boolean') throw invalid(); }
      else if (value.op === 'confidence_gte' || value.op === 'confidence_lt') {
        if (typeof value.value !== 'number' || value.value < 0 || value.value > 1) throw invalid();
      } else throw invalid();
    }
    reference(value, index, current);
  }
  function target(value: unknown, index: number): void {
    if (value === null) return;
    if (typeof value !== 'string' || !indices.has(value) || indices.get(value)! <= index) throw invalid();
  }
  function template(value: unknown, index: number): void {
    if (!record(value) || Object.keys(value).length !== 1) throw invalid();
    if (Object.hasOwn(value, 'literal')) return;
    if (Object.hasOwn(value, 'state')) { if (value.state !== true) throw invalid(); return; }
    if (Object.hasOwn(value, 'answer')) {
      exact(value.answer, ['step', 'question', 'field']);
      if (typeof value.answer.field !== 'string' || !['decision', 'confidence', 'alternatives'].includes(value.answer.field)) throw invalid();
      reference(value.answer, index, false); return;
    }
    if (Object.hasOwn(value, 'object')) {
      if (!record(value.object)) throw invalid();
      for (const child of Object.values(value.object)) template(child, index);
      return;
    }
    if (Object.hasOwn(value, 'array')) {
      if (!Array.isArray(value.array)) throw invalid();
      for (const child of value.array) template(child, index);
      return;
    }
    throw invalid();
  }
  try {
    for (const [index, step] of definition.steps.entries()) {
      if (step.depends_on !== undefined) {
        if (!Array.isArray(step.depends_on)) throw invalid();
        for (const item of step.depends_on) condition(item, index, false);
      }
      if (step.state_template !== undefined) template(step.state_template, index);
      exact(step.next, ['rules', 'default']);
      if (!Array.isArray(step.next.rules)) throw invalid();
      target(step.next.default, index);
      for (const rule of step.next.rules) { exact(rule, ['when', 'next']); condition(rule.when, index, true); target(rule.next, index); }
    }
  } catch { throw invalid(); }
  return definition;
}

type SavedAnswers = Map<string, Record<string, DecisionResult>>;
function matches(condition: FlowCondition, answers: SavedAnswers): boolean {
  const result = answers.get(condition.step)?.[condition.question];
  if (!result || result.kind === 'refusal') return false;
  if (condition.op === 'eq') return result.decision === condition.value;
  if (condition.op === 'in') return condition.values.includes(result.decision);
  if (result.confidence === null) return false;
  return condition.op === 'confidence_gte' ? result.confidence >= condition.value : result.confidence < condition.value;
}
function render(template: FlowTemplate, state: JsonValue, answers: SavedAnswers): JsonValue {
  if ('literal' in template) return template.literal;
  if ('state' in template) return state;
  if ('answer' in template) {
    const result = answers.get(template.answer.step)?.[template.answer.question];
    if (!result || result.kind === 'refusal') throw invalid();
    return snapshot(result[template.answer.field]);
  }
  if ('array' in template) return template.array.map(child => render(child, state, answers));
  return Object.fromEntries(Object.entries(template.object).map(([name, child]) => [name, render(child, state, answers)]));
}
function usageOf(reply: SystemZeroReply): Record<string, number> | null {
  if (!record(reply.usage)) return null;
  return Object.fromEntries(usageFields.flatMap(name => {
    const value: unknown = reply.usage[name as keyof typeof reply.usage];
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? [[name, value]] : [];
  }));
}
function refusal(code: string, requestId: string | null): SystemZeroError {
  const held = heldCodes.has(code);
  const message = held ? heldMessages[code] : Object.hasOwn(questionRefusals, code) ? questionRefusals[code] : undefined;
  if (!message) return unavailable(requestId);
  return responseError(held ? 404 : 422, new Headers(), {
    error: { code, message, type: held ? 'not_found_error' : 'invalid_request_error', retryable: false }, request_id: requestId,
  }, false);
}
function normalizeError(error: unknown, requestId: string | null): SystemZeroError {
  if (error instanceof APIConnectionError && !(error instanceof APITimeoutError)) return error;
  if (error instanceof SystemZeroError && error.code !== null) return error;
  if (error instanceof APITimeoutError) return timeout(error.request_id ?? requestId);
  return unavailable(error instanceof SystemZeroError ? error.request_id ?? requestId : requestId);
}
function errorTrace(error: SystemZeroError): FlowErrorTrace {
  return {
    code: typeof error.code === 'string' && (knownCodes.has(error.code) || /^[a-z][a-z0-9_]{0,63}$/u.test(error.code))
      ? error.code : 'service_unavailable',
    type: error.type !== null && errorTypes.has(error.type) ? error.type : 'api_error',
    status: typeof error.status === 'number' && Number.isInteger(error.status) ? error.status : null,
    retryable: error.retryable === true,
  };
}

/** Execute ordinary counted decide calls. All returned diagnostics omit content by default. */
export async function runFlow(client: Pick<SystemZeroClient, 'decide' | 'options'>, input: unknown, options: FlowOptions): Promise<FlowTrace> {
  if (!record(options)) throw invalid();
  if (options.dangerZone !== undefined && options.dangerZone !== 'accept') throw new InvalidRequestError('The request option must be accept.', { status: 400, type: 'invalid_request_error', code: 'invalid_request', param: 'SZ-Danger-Zone' });
  let settings: FlowOptions;
  try { settings = { ...options }; } catch { throw invalid(); }
  if (settings.now !== undefined && typeof settings.now !== 'function') throw invalid();
  const now = settings.now ?? (() => performance.now());
  let lastTime = 0;
  function time(): number {
    let value: unknown;
    try { value = now(); } catch { throw invalid(); }
    if (typeof value !== 'number' || !Number.isFinite(value) || value < lastTime) throw invalid();
    lastTime = value; return value;
  }
  const started = time();
  const definition = validateFlow(input);
  const trace: FlowTrace = { version: 1, run_hash: '', status: 'completed', started_ms: started, elapsed_ms: 0, decide_calls: 0, steps: [] };
  const maxDecideCalls = settings.max_decide_calls === undefined ? MAX_STEPS : settings.max_decide_calls;
  let current: FlowStepTrace | undefined;
  let phase: 'local' | 'transport' | 'reply' | 'refusal' = 'local';
  try {
    checkRunId(settings.run_id);
    trace.run_hash = digest(settings.run_id);
    if (!Number.isSafeInteger(maxDecideCalls) || maxDecideCalls < 0 || maxDecideCalls > MAX_STEPS) {
      throw new InvalidRequestError('max_decide_calls must be a nonnegative safe integer at most 32.', { code: 'invalid_request', type: 'invalid_request_error', status: 400 });
    }
    if ( !Number.isInteger(settings.deadline_ms) || settings.deadline_ms < 1 || settings.deadline_ms > 2_147_483_647
      || (settings.include_content !== undefined && typeof settings.include_content !== 'boolean')) throw invalid();
    const includeContent = settings.include_content === true;
    const countBound = client.options.timeout * (client.options.maxRetries + 1) + client.options.maxRetryWait * client.options.maxRetries;
    const callBound = client.options.maxRetries > 0 && client.options.retryBudget > 0
      ? Math.min(countBound, Math.max(client.options.timeout, client.options.retryBudget)) : client.options.timeout;
    if (!Number.isFinite(callBound) || callBound < 1) throw invalid();
    const indices = new Map(definition.steps.map((step, index) => [step.id, index]));
    const answers: SavedAnswers = new Map();
    let state: JsonValue = definition.request.state;
    let index: number | null = 0;
    function remaining(): number { return settings.deadline_ms - (time() - started); }
    while (index !== null) {
      if (trace.steps.length >= MAX_STEPS) throw invalid();
      phase = 'local';
      const step: FlowStep = definition.steps[index]!;
      const questionNames = Object.keys(step.questions).sort((left, right) => Buffer.compare(Buffer.from(left), Buffer.from(right)));
      current = {
        step_index: index, status: 'failed', request_id: null, request_hash: null, idempotency_key: null,
        question_count: questionNames.length, started_ms: time(), elapsed_ms: 0, decide_calls: 0,
        replayed: false, usage: null, answers: [], branch: null,
      };
      if (includeContent) { current.step_id = step.id; current.questions = step.questions; }
      trace.steps.push(current);
      if (remaining() <= 0) throw timeout();
      if (!(step.depends_on ?? []).every(condition => matches(condition, answers))) {
        index = step.next.default === null ? null : indices.get(step.next.default)!;
        current.status = 'skipped';
        current.branch = { rule_index: null, next_step_index: index, reason: 'dependency_unmet' };
        current.elapsed_ms = time() - current.started_ms;
        if (remaining() <= 0) throw timeout();
        continue;
      }
      if (step.state_template !== undefined) state = render(step.state_template, state, answers);
      const request = { ...definition.request, state, questions: step.questions,
        ...(step.attachments !== undefined ? { attachments: step.attachments } : {}) } as DecideRequest;
      const requestHash = flowRequestHash(request);
      const idempotencyKey = `sz-flow-v1-${digest([settings.run_id, step.id, requestHash])}`;
      current.request_hash = requestHash; current.idempotency_key = idempotencyKey;
      if (includeContent) current.request = request;
      if (remaining() < callBound) throw timeout();
      if (trace.decide_calls >= maxDecideCalls) {
        throw new SystemZeroError('The local call limit has been reached.', { code: 'call_limit_reached', retryable: false });
      }
      trace.decide_calls++;
      current.decide_calls = 1;
      phase = 'transport';
      const response = await client.decide(request, withRetryContext({ idempotencyKey, ...(settings.dangerZone === undefined ? {} : { dangerZone: settings.dangerZone }) }, { workflowRecovery: true })).withResponse();
      phase = 'reply';
      current.request_id = response.metadata.requestId;
      current.replayed = response.metadata.replayed;
      const reply = response.data;
      // The parsed clone is complete; flows do not expose the unread tee branch.
      await response.response.body?.cancel().catch(() => {});
      if (!record(reply) || !record(reply.answers) || Object.keys(reply.answers).length !== questionNames.length
        || questionNames.some(name => !Object.hasOwn(reply.answers, name))) throw unavailable(current.request_id);
      current.usage = usageOf(reply);
      let decisions: Record<string, DecisionResult>;
      try { decisions = decisionsFor(reply, request); } catch { throw unavailable(current.request_id); }
      answers.set(step.id, decisions);
      current.answers = questionNames.map((name, questionIndex) => {
        const decision = decisions[name]!;
        const result: FlowAnswerTrace = { question_index: questionIndex, kind: decision.kind, confidence: decision.kind === 'refusal' ? null : decision.confidence };
        if (includeContent) {
          result.question = name;
          if (decision.kind !== 'refusal') { result.decision = decision.decision; result.alternatives = snapshot(decision.alternatives) as unknown as Alternative[]; }
        }
        return result;
      });
      for (const name of questionNames) {
        const result = decisions[name]!;
        if (result.kind === 'refusal') { phase = 'refusal'; throw refusal(result.code, current.request_id); }
      }
      phase = 'local';
      let next = step.next.default;
      let ruleIndex: number | null = null;
      for (const [candidateIndex, rule] of step.next.rules.entries()) {
        if (matches(rule.when, answers)) { next = rule.next; ruleIndex = candidateIndex; break; }
      }
      index = next === null ? null : indices.get(next)!;
      current.branch = { rule_index: ruleIndex, next_step_index: index, reason: ruleIndex === null ? 'default' : 'rule_matched' };
      current.elapsed_ms = time() - current.started_ms;
      if (remaining() <= 0) throw timeout(current.request_id);
      current.status = 'completed';
    }
    trace.elapsed_ms = time() - started;
    if (trace.elapsed_ms >= settings.deadline_ms) throw timeout(current?.request_id ?? null);
    return trace;
  } catch (cause) {
    const error = normalizeError(cause, current?.request_id ?? null);
    const status = heldCodes.has(error.code ?? '') ? 'held'
      : (phase === 'transport' || phase === 'refusal') && error.status !== null && error.status >= 400 && error.status < 500 ? 'refused' : 'failed';
    trace.status = status;
    try { time(); } catch { /* Keep the last valid monotonic timestamp. */ }
    trace.elapsed_ms = lastTime - started;
    if (current) {
      current.status = status;
      current.request_id = error.request_id ?? current.request_id;
      current.elapsed_ms = lastTime - current.started_ms;
      current.error = errorTrace(error);
    }
    throw redactClientDiagnostics(client, Object.assign(error, { trace }));
  }
}
