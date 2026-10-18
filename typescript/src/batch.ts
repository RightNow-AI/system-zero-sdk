import { createHash } from 'node:crypto';
import { appendFileSync, closeSync, existsSync, fsyncSync, openSync, readFileSync, ftruncateSync, unlinkSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import type { DecideRequest } from './api-types.js';
import { checkedBaseURL, withRetryContext, type SystemZeroClient } from './client.js';
import { InvalidRequestError, SystemZeroError } from './errors.js';
import { SYSTEM_ZERO_ERROR_CODES, GATEWAY_ERROR_CODES } from './public-error-codes.js';
import { scoreReply, scoreRefusal, validateExpected, evaluate, type QuestionScore, type EvaluationRecord } from './evaluate.js';

export interface BatchItem { id: string; request: DecideRequest; expected: Record<string, boolean | string | null> }
interface Admission {
  schema: 1; run_id: string; id: string; request_hash: string; expected_hash: string; idempotency_key: string;
  admitted_at_ms: number; target: string;
}
type BatchUsage = NonNullable<EvaluationRecord['usage']>;
export interface BatchRecord extends Admission {
  outcome: 'completed' | 'held' | 'error' | 'refused'; code: string | null; attempts: number; latency_ms: number;
  usage: BatchUsage | null; questions: QuestionScore[];
}
export interface BatchOptions {
  runId: string; resultsPath: string; maxDecideCalls: number; deadlineMs: number; concurrency?: number; maxRetries?: number;
  now?: () => number; random?: () => number; sleep?: (milliseconds: number) => Promise<void>;
}
export interface BatchResult { records: BatchRecord[]; stopped: 'call_limit' | 'deadline' | null }
const REPLAY_MS = 86_400_000;
const CONFLICT = new Set(['idempotency_conflict']);
const HELD = new Set(['long_context_not_available', 'long_recording_not_available', 'video_not_available', 'image_not_available', 'audio_not_available', 'document_not_available', 'committee_not_available', 'deliberate_not_available']);
const REFUSED = new Set(['injection_suspected', 'request_refused', 'question_not_grounded']);
const KNOWN = new Set<string>([...SYSTEM_ZERO_ERROR_CODES, ...GATEWAY_ERROR_CODES]);
function invalid(param: string | null = null): never { throw new InvalidRequestError('Invalid batch input or checkpoint.', { status: 400, type: 'invalid_request_error', code: 'invalid_request', param }); }
function count(n: unknown): n is number { return typeof n === 'number' && Number.isSafeInteger(n) && n >= 0; }
function safeId(id: unknown): id is string { return typeof id === 'string' && /^[A-Za-z0-9_.-]{1,128}$/u.test(id); }
/** Ordered, length-framed JSON values; doubles use their exact IEEE754 bits. */
export function requestHash(value: unknown): string {
  const hash = createHash('sha256');
  const visit = (v: unknown): void => {
    if (v === null) { hash.update('n'); return; }
    if (typeof v === 'boolean') { hash.update(v ? 't' : 'f'); return; }
    if (typeof v === 'number') {
      if (!Number.isFinite(v) || (Number.isInteger(v) && !Number.isSafeInteger(v))) invalid();
      const bytes = Buffer.alloc(8); bytes.writeDoubleBE(v === 0 ? 0 : v); hash.update('d' + bytes.toString('hex')); return;
    }
    if (typeof v === 'string') {
      // Lone surrogates do not have a shared UTF-8 representation.
      if (Buffer.from(v).toString('utf8') !== v) invalid();
      hash.update('s' + Buffer.byteLength(v) + ':'); hash.update(v); return;
    }
    if (Array.isArray(v)) { hash.update('a' + v.length + ':'); for (const entry of v) visit(entry); return; }
    if (v && typeof v === 'object' && Object.getPrototypeOf(v) === Object.prototype) {
      const entries = Object.entries(v); hash.update('o' + entries.length + ':'); for (const [key, entry] of entries) { visit(key); visit(entry); } return;
    }
    invalid();
  };
  try { visit(value); } catch { invalid(); }
  return hash.digest('hex');
}
export function idempotencyKey(runId: string, id: string, hash: string): string {
  if (!safeId(runId) || !safeId(id) || !/^[a-f0-9]{64}$/u.test(hash)) invalid();
  return 'szbatch_' + createHash('sha256').update(runId + '\0' + id + '\0' + hash).digest('hex');
}

function replyUsage(reply: unknown): BatchUsage | null {
  const source = (reply as { usage?: unknown })?.usage;
  if (!source || typeof source !== 'object') return null;
  const usage: Record<string, number> = {};
  for (const key of ['input_tokens', 'cached_input_tokens', 'media_tokens', 'long_input_tokens', 'media_items', 'thought_tokens']) {
    const n = (source as Record<string, unknown>)[key]; if (!count(n)) return null; usage[key] = n;
  }
  const uncached = usage.input_tokens! - usage.cached_input_tokens! - usage.media_tokens!;
  if (uncached < usage.long_input_tokens!) return null;
  return usage as BatchUsage;
}
function append(path: string, row: unknown): void {
  const fd = openSync(path, 'a', 0o600);
  try { appendFileSync(fd, JSON.stringify(row) + '\n'); fsyncSync(fd); } finally { closeSync(fd); }
}
function readRows<T>(path: string): T[] {
  if (!existsSync(path)) return [];
  const bytes = readFileSync(path); const end = bytes.lastIndexOf(10) + 1;
  if (end !== bytes.length) { const fd = openSync(path, 'r+'); try { ftruncateSync(fd, end); fsyncSync(fd); } finally { closeSync(fd); } }
  try { return bytes.subarray(0, end).toString('utf8').split('\n').filter(Boolean).map(line => JSON.parse(line) as T); } catch { invalid(); }
}
/** Offline persistence plus ordinary decide calls. Use one results path and API key per run. */
export async function runBatch(client: SystemZeroClient, input: readonly BatchItem[], options: BatchOptions): Promise<BatchResult> {
  // One elapsed deadline includes preparation and is shared by every item.
  const started = performance.now();
  const now = options.now ?? Date.now;
  const concurrency = options.concurrency ?? 2; const retries = options.maxRetries ?? client.options.maxRetries;
  if (!safeId(options.runId) || !options.resultsPath || !Number.isFinite(options.deadlineMs) || options.deadlineMs <= 0 || options.deadlineMs > 2_147_483_647 || !count(retries) || ![1, 2].includes(concurrency)) invalid();
  let target: string; try { target = checkedBaseURL(client.options.baseURL); } catch { invalid(); }
  const targetHash = createHash('sha256').update(target).digest('hex');
  const wallStarted = now(); if (!Number.isFinite(wallStarted) || wallStarted < 0 || wallStarted > Number.MAX_SAFE_INTEGER - options.deadlineMs) invalid();
  if (!count(options.maxDecideCalls)) invalid('max_decide_calls');
  const deadline = started + options.deadlineMs;
  const ids = new Set<string>();
  const prepared = input.map(item => {
    if (!safeId(item.id) || ids.has(item.id)) invalid(); ids.add(item.id);
    requestHash(item.request); requestHash(item.expected);
    const copy = JSON.parse(JSON.stringify(item)) as BatchItem;
    validateExpected(copy.request, copy.expected);
    const hash = requestHash(copy.request);
    return { item: copy, admission: { schema: 1 as const, run_id: options.runId, id: item.id, request_hash: hash, expected_hash: requestHash(copy.expected), idempotency_key: idempotencyKey(options.runId, item.id, hash), admitted_at_ms: 0, target: targetHash } };
  });
  const lock = options.resultsPath + '.lock'; let lockFd: number;
  try { mkdirSync(dirname(options.resultsPath), { recursive: true }); lockFd = openSync(lock, 'wx', 0o600); } catch { invalid(); }
  try {
    const journalPath = options.resultsPath + '.admissions.jsonl';
    const journal = readRows<Admission>(journalPath); const records = readRows<BatchRecord>(options.resultsPath);
    const byId = new Map(prepared.map(entry => [entry.item.id, entry])); const admitted = new Map<string, Admission>(); const completed = new Map<string, BatchRecord>();
    for (const a of journal) {
      const wanted = byId.get(a?.id)?.admission;
      if (!wanted || admitted.has(a.id) || !count(a.admitted_at_ms) || Object.keys(a).length !== Object.keys(wanted).length || Object.keys(wanted).some(key => key !== 'admitted_at_ms' && a[key as keyof Admission] !== wanted[key as keyof Admission])) invalid();
      admitted.set(a.id, a);
    }
    for (const row of records) {
      const a = admitted.get(row?.id);
      if (!a || completed.has(row.id) || Object.keys(a).some(key => row[key as keyof Admission] !== a[key as keyof Admission]) || !count(row.attempts) || !Number.isFinite(row.latency_ms) || row.latency_ms < 0 || !Array.isArray(row.questions) || !['completed', 'held', 'error', 'refused'].includes(row.outcome)) invalid();
      const allowed = new Set([...Object.keys(a), 'outcome', 'code', 'attempts', 'latency_ms', 'usage', 'questions']);
      if (Object.keys(row).length !== allowed.size || Object.keys(row).some(key => !allowed.has(key))) invalid();
      if (row.outcome === 'error' || row.outcome === 'held') { if (row.questions.length) invalid(); }
      else {
        const wantedQuestions = Object.keys(byId.get(row.id)!.item.request.questions);
        if (row.questions.length !== wantedQuestions.length || row.questions.some(q => !wantedQuestions.includes(q.question_id))) invalid();
      }
      if (row.outcome === 'held') {
        if (!row.code || !HELD.has(row.code) || row.usage !== null || row.questions.length) invalid();
      } else if (row.outcome === 'completed') {
        if (row.code !== null || (row.usage !== null && replyUsage({ usage: row.usage }) === null)) invalid();
      } else if (row.usage !== null) invalid();
      completed.set(row.id, row);
    }
    evaluate(records);
    if (admitted.size > options.maxDecideCalls) invalid('max_decide_calls');
    let stopped: BatchResult['stopped'] = null; let cursor = 0; let failed = false;
    const pending = prepared.filter(entry => !completed.has(entry.item.id));
    // A completed local row never needs server replay. An uncertain expired row must never dispatch anew.
    for (const entry of pending) { const a = admitted.get(entry.item.id); if (a && (now() < a.admitted_at_ms || now() + client.options.timeout >= a.admitted_at_ms + REPLAY_MS)) invalid(); }
    const worker = async (): Promise<void> => {
      while (!failed && !stopped && cursor < pending.length) {
        const entry = pending[cursor]!;
        let a = admitted.get(entry.item.id);
        if (!a && admitted.size >= options.maxDecideCalls) { stopped = 'call_limit'; return; }
        if (performance.now() + client.options.timeout > deadline) { stopped = 'deadline'; return; }
        if (!a) {
          a = { ...entry.admission, admitted_at_ms: Math.floor(now()) }; append(journalPath, a); admitted.set(a.id, a);
        }
        cursor++;
        const started = performance.now();
        const row: BatchRecord = { ...a, outcome: 'error', code: 'service_unavailable', attempts: 0, latency_ms: 0, usage: null, questions: [] };
        if (performance.now() + client.options.timeout > deadline || now() + client.options.timeout >= a.admitted_at_ms + REPLAY_MS) {
          stopped = 'deadline'; row.code = 'system_zero_timeout';
        } else {
          try {
            // The transport owns the only recovery policy. Admission can narrow
            // its count/time bounds but cannot reset its monotonic call budget.
            const reply = await client.decide(entry.item.request, withRetryContext({ idempotencyKey: a.idempotency_key }, {
              workflowRecovery: true,
              maxRetries: retries,
              onAttempt: () => { row.attempts++; },
              remaining: () => Math.min(deadline - performance.now(), a.admitted_at_ms + REPLAY_MS - now()),
              onDeadline: () => { stopped = 'deadline'; },
              ...(options.sleep === undefined ? {} : { sleep: options.sleep }),
              ...(options.random === undefined ? {} : { random: () => {
                const random = options.random!();
                if (!(Number.isFinite(random) && random >= 0 && random <= 1)) invalid();
                return random;
              } }),
            }));
            const usage = replyUsage(reply);
            row.questions = scoreReply(entry.item.request, entry.item.expected, reply);
            row.outcome = 'completed'; row.code = null;
            row.usage = usage;
          } catch (error) {
            row.code = error instanceof SystemZeroError && error.code && (KNOWN.has(error.code) || CONFLICT.has(error.code)) ? error.code : 'service_unavailable';
            if (error instanceof SystemZeroError && error.status === 404 && HELD.has(row.code)) { row.outcome = 'held'; }
            else if (error instanceof SystemZeroError && error.status === 422 && REFUSED.has(row.code)) {
              row.outcome = 'refused'; row.questions = scoreRefusal(entry.item.request, entry.item.expected, row.code);
            }
          }
        }
        row.latency_ms = Math.max(0, performance.now() - started);
        append(options.resultsPath, row); completed.set(row.id, row);
      }
    };
    const workers = await Promise.allSettled(Array.from({ length: concurrency }, async () => {
      try { await worker(); } catch (error) { failed = true; throw error; }
    }));
    if (workers.some(result => result.status === 'rejected')) invalid();
    return { records: prepared.flatMap(entry => { const row = completed.get(entry.item.id); return row ? [row] : []; }), stopped };
  } catch (error) { if (error instanceof SystemZeroError) throw error; invalid(); }
  finally { closeSync(lockFd); unlinkSync(lock); }
}
