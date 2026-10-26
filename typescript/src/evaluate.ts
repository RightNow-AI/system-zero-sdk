import { Buffer } from 'node:buffer';
import { createHash } from 'node:crypto';
import type { DecideRequest, Question, QuestionRefusalData, V1DecideReply } from './api-types.js';
import { decisionsFor, type SystemZeroReply } from './decisions.js';
import { ERROR_CODES, type KnownErrorCode } from './error-codes.js';
import { APIError, InvalidRequestError } from './errors.js';

export type ExpectedAnswers = Record<string, boolean | string | null>;
export type QuestionType = Question['type'];
export type Usage = V1DecideReply['usage'];
export interface QuestionScore {
  question_id: string;
  type: QuestionType;
  outcome: 'correct' | 'wrong' | 'refused';
  expected_refusal: boolean;
  correct: boolean;
  confidence: number | null;
  choice_expected: number | null;
  choice_actual: number | null;
  choice_schema: string | null;
  code: KnownErrorCode | null;
}
export interface EvaluationRecord {
  id: string;
  outcome: 'completed' | 'held' | 'error' | 'refused';
  questions: QuestionScore[];
  latency_ms: number;
  usage: Usage | null;
  code: string | null;
}
export interface AccuracyMetrics {
  questions: number;
  correct: number;
  wrong: number;
  refused: number;
  expected_answers: number;
  false_refusals: number;
  accuracy: number | null;
  false_refusal_rate: number | null;
}
export interface ReliabilityBin {
  lower: number;
  upper: number;
  count: number;
  accuracy: number | null;
  mean_confidence: number | null;
}
export interface EvaluationReport {
  version: 1;
  items: Record<'total' | 'completed' | 'refused' | 'held' | 'error', number>;
  metrics: AccuracyMetrics;
  by_type: Record<QuestionType, AccuracyMetrics>;
  choice_confusion: { question_id: string; choice_schema: string; counts: { expected: number; actual: number; count: number }[] }[];
  latency_ms: { count: number; p50: number | null; p95: number | null };
  usage: { totals: Usage; records_with_usage: number; records_without_usage: number };
  calibration: {
    scope: 'caller_data_only';
    label: string;
    method: 'selected_outcome_correctness';
    count: number;
    bins: ReliabilityBin[];
    ece: number;
    brier_score: number;
  } | null;
  records: EvaluationRecord[];
}
const knownCodes = new Set<string>(Object.values(ERROR_CODES));
 
const questionRefusalCodes = new Set<string>([
  'injection_suspected', 'question_not_grounded', ] satisfies QuestionRefusalData['error']['code'][]);
const semanticRefusalCodes = new Set([...questionRefusalCodes, 'request_refused']);
const types: readonly QuestionType[] = ['boolean', 'choice', 'score'];
const usageFields = ['input_tokens', 'cached_input_tokens', 'media_tokens', 'long_input_tokens', 'media_items', 'thought_tokens'] as const;
const blankUsage = (): Usage => ({ input_tokens: 0, cached_input_tokens: 0, media_tokens: 0, long_input_tokens: 0, media_items: 0, thought_tokens: 0 });
function inputError(): never {
  throw new InvalidRequestError('Invalid batch evaluation input.', { code: 'invalid_request', status: 400, type: 'invalid_request_error' });
}
function replyError(): never {
  throw new APIError('The decision reply cannot be evaluated.', { code: 'service_unavailable', status: 503, type: 'api_error' });
}
function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
function nonnegative(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}
function integer(value: unknown): value is number {
  return nonnegative(value) && Number.isSafeInteger(value);
}
function add(a: number, b: number): number {
  const result = a + b;
  if (!Number.isSafeInteger(result)) inputError();
  return result;
}
function utf8Order(a: string, b: string): number {
  return Buffer.compare(Buffer.from(a), Buffer.from(b));
}
function asciiJson(value: unknown, pretty = false): string {
  return JSON.stringify(value, null, pretty ? 2 : undefined).replace(/[\u0080-\uffff]/g, character =>
    '\\u' + character.charCodeAt(0).toString(16).padStart(4, '0'));
}
function choices(question: Question): string[] {
  return question.type === 'choice' ? Object.keys(question.criteria).sort(utf8Order) : [];
}
function choiceSchema(labels: string[]): string {
  return createHash('sha256').update(asciiJson(labels), 'utf8').digest('hex');
}

/** Only labels are checked here. Decide remains responsible for request and media limits. */
export function validateExpected(request: Pick<DecideRequest, 'questions'>, expected: unknown): asserts expected is ExpectedAnswers {
  if (!object(request) || !object(request.questions) || !object(expected)) inputError();
  const names = Object.keys(request.questions);
  if (names.length !== Object.keys(expected).length || names.some(name => !Object.hasOwn(expected, name))) inputError();
  for (const name of names) {
    const question = request.questions[name];
    if (!object(question) || !types.includes(question.type)) inputError();
    const value = expected[name];
    if (value === null) continue;
    if (question.type === 'boolean') {
      if (typeof value !== 'boolean') inputError();
    } else if (question.type === 'choice') {
      if (!object(question.criteria) || typeof value !== 'string' || !Object.hasOwn(question.criteria, value)) inputError();
    } else if (!Array.isArray(question.criteria) || typeof value !== 'string' ||
      !/^(0|[1-9][0-9]*)$/u.test(value) || !Number.isSafeInteger(Number(value)) || Number(value) >= question.criteria.length) inputError();
  }
}

function refusalScore(question_id: string, question: Question, wanted: boolean | string | null, code: KnownErrorCode): QuestionScore {
  const labels = choices(question);
  return {
    question_id, type: question.type, outcome: 'refused', expected_refusal: wanted === null,
    correct: wanted === null, confidence: null,
    choice_expected: question.type === 'choice' && typeof wanted === 'string' ? labels.indexOf(wanted) : null,
    choice_actual: null, choice_schema: question.type === 'choice' ? choiceSchema(labels) : null, code,
  };
}

/** Score a typed whole-request semantic refusal; held and operational codes are not quality answers. */
export function scoreRefusal(request: Pick<DecideRequest, 'questions'>, expected: ExpectedAnswers, code: string): QuestionScore[] {
  validateExpected(request, expected);
  if (!semanticRefusalCodes.has(code)) replyError();
  try {
    return Object.keys(request.questions).sort(utf8Order).map(question_id =>
      refusalScore(question_id, request.questions[question_id]!, expected[question_id]!, code as KnownErrorCode));
  } catch {
    return replyError();
  }
}

/** Project only scoring facts. Request text, labels, attachment bytes and raw replies are discarded. */
export function scoreReply(request: Pick<DecideRequest, 'questions'>, expected: ExpectedAnswers, reply: SystemZeroReply): QuestionScore[] {
  validateExpected(request, expected);
  try {
    const names = Object.keys(request.questions).sort(utf8Order);
    if (!object(reply) || !object(reply.answers) || names.length !== Object.keys(reply.answers).length ||
      names.some(name => !Object.hasOwn(reply.answers, name))) replyError();
    const results = decisionsFor(reply, request);
    return names.map(question_id => {
      const question = request.questions[question_id]!;
      const result = results[question_id]!;
      const wanted = expected[question_id]!;
      const labels = choices(question);
      const schema = question.type === 'choice' ? choiceSchema(labels) : null;
      if (result.kind === 'refusal') {
        if (!questionRefusalCodes.has(result.code)) replyError();
        return refusalScore(question_id, question, wanted, result.code as KnownErrorCode);
      }
      const raw = reply.answers[question_id]!;
      if ('error' in raw || raw.type !== question.type) replyError();
      if (question.type !== 'boolean') {
        if (!('probabilities' in raw)) replyError();
        const expectedKeys = question.type === 'choice' ? labels : question.criteria.map((_, index) => String(index));
        const actualKeys = Object.keys(raw.probabilities);
        if (actualKeys.length !== expectedKeys.length || expectedKeys.some(key => !Object.hasOwn(raw.probabilities, key))) replyError();
      }
      const correct = result.decision === wanted;
      const actual = question.type === 'choice' ? labels.indexOf(String(result.decision)) : null;
      if (actual !== null && actual < 0) replyError();
      return {
        question_id, type: question.type, outcome: correct ? 'correct' : 'wrong',
        expected_refusal: wanted === null, correct, confidence: result.confidence,
        choice_expected: question.type === 'choice' && typeof wanted === 'string' ? labels.indexOf(wanted) : null,
        choice_actual: actual, choice_schema: schema, code: null,
      };
    });
  } catch {
    return replyError();
  }
}
function projectQuestion(value: QuestionScore): QuestionScore {
  if (!object(value) || typeof value.question_id !== 'string' || !types.includes(value.type) ||
    !['correct', 'wrong', 'refused'].includes(value.outcome) || typeof value.correct !== 'boolean' ||
    typeof value.expected_refusal !== 'boolean' ||
    (value.confidence !== null && (!nonnegative(value.confidence) || value.confidence > 1 ||
      value.confidence !== Math.round(value.confidence * 1_000_000) / 1_000_000)) ||
    (value.code !== null && !knownCodes.has(value.code))) inputError();
  const refused = value.outcome === 'refused';
  if (value.correct !== (refused ? value.expected_refusal : value.outcome === 'correct') ||
    (refused && (value.confidence !== null || value.code === null || !semanticRefusalCodes.has(value.code))) || (!refused && value.code !== null) ||
    (value.expected_refusal && !refused && value.correct)) inputError();
  for (const index of [value.choice_expected, value.choice_actual]) if (index !== null && !integer(index)) inputError();
  if (value.type === 'choice') {
    if (typeof value.choice_schema !== 'string' || !/^[a-f0-9]{64}$/u.test(value.choice_schema) ||
      (value.expected_refusal !== (value.choice_expected === null)) || (refused !== (value.choice_actual === null))) inputError();
  } else if (value.choice_expected !== null || value.choice_actual !== null || value.choice_schema !== null) inputError();
  return {
    question_id: value.question_id, type: value.type, outcome: value.outcome,
    expected_refusal: value.expected_refusal, correct: value.correct, confidence: value.confidence,
    choice_expected: value.choice_expected, choice_actual: value.choice_actual, choice_schema: value.choice_schema, code: value.code,
  };
}
function projectRecord(value: EvaluationRecord): EvaluationRecord {
  if (!object(value) || typeof value.id !== 'string' ||
    !['completed', 'refused', 'held', 'error'].includes(value.outcome) || !nonnegative(value.latency_ms) ||
    !Array.isArray(value.questions) ||
    (value.code !== null && (typeof value.code !== 'string' || (!knownCodes.has(value.code)
      && !(value.outcome === 'error' && /^[a-z][a-z0-9_]{0,63}$/u.test(value.code)))))) inputError();
  let usage: Usage | null = null;
  if (value.usage !== null) {
    if (!object(value.usage)) inputError();
    usage = blankUsage();
    for (const key of usageFields) {
      if (!integer(value.usage[key])) inputError();
      usage[key] = value.usage[key];
    }
  }
  const questions = value.outcome === 'held' || value.outcome === 'error' ? [] : value.questions.map(projectQuestion);
  if (new Set(questions.map(row => row.question_id)).size !== questions.length) inputError();
  return {
    id: value.id, outcome: value.outcome, questions, latency_ms: value.latency_ms, usage,
    code: value.code,
  };
}
function accuracy(rows: QuestionScore[]): AccuracyMetrics {
  const correct = rows.filter(row => row.correct).length;
  const expectedAnswers = rows.filter(row => !row.expected_refusal).length;
  const falseRefusals = rows.filter(row => !row.expected_refusal && row.outcome === 'refused').length;
  return {
    questions: rows.length, correct, wrong: rows.filter(row => row.outcome === 'wrong').length,
    refused: rows.filter(row => row.outcome === 'refused').length, expected_answers: expectedAnswers,
    false_refusals: falseRefusals, accuracy: rows.length ? correct / rows.length : null,
    false_refusal_rate: expectedAnswers ? falseRefusals / expectedAnswers : null,
  };
}
/** Linear interpolation at (n - 1) * p; refusals count, held calls and errors do not. */
function percentile(sorted: number[], p: number): number | null {
  if (!sorted.length) return null;
  const index = (sorted.length - 1) * p;
  const lower = Math.floor(index);
  return sorted[lower]! + (sorted[Math.ceil(index)]! - sorted[lower]!) * (index - lower);
}

/** Calibration concerns selected-outcome correctness on the caller's labels, never a population claim. */
export function evaluate(records: readonly EvaluationRecord[], options: { bins?: number } = {}): EvaluationReport {
  const bins = options.bins ?? 10;
  if (!Array.isArray(records) || !Number.isInteger(bins) || bins < 1 || bins > 100) inputError();
  const projected = records.map(projectRecord);
  if (new Set(projected.map(row => row.id)).size !== projected.length) inputError();
  const quality = projected.filter(row => row.outcome === 'completed' || row.outcome === 'refused');
  const questions = quality.flatMap(row => row.questions);
  const latency = quality.map(row => row.latency_ms).sort((a, b) => a - b);
  const totals = blankUsage();
  let withUsage = 0;
  for (const row of projected) {
    if (row.usage !== null) {
      withUsage++;
      for (const key of usageFields) totals[key] = add(totals[key], row.usage[key]);
    }
  }
  const confusion = new Map<string, EvaluationReport['choice_confusion'][number]>();
  for (const row of questions) {
    if (row.type !== 'choice' || row.choice_expected === null || row.choice_actual === null) continue;
    const key = asciiJson([row.question_id, row.choice_schema]);
    let group = confusion.get(key);
    if (!group) {
      group = { question_id: row.question_id, choice_schema: row.choice_schema!, counts: [] };
      confusion.set(key, group);
    }
    const cell = group.counts.find(count => count.expected === row.choice_expected && count.actual === row.choice_actual);
    if (cell) cell.count++;
    else group.counts.push({ expected: row.choice_expected, actual: row.choice_actual, count: 1 });
  }
  const choiceConfusion = [...confusion.values()].sort((a, b) => utf8Order(a.question_id, b.question_id) || utf8Order(a.choice_schema, b.choice_schema));
  for (const group of choiceConfusion) group.counts.sort((a, b) => a.expected - b.expected || a.actual - b.actual);
  const confidenceRows = questions.filter(row => row.confidence !== null);
  let calibration: EvaluationReport['calibration'] = null;
  if (confidenceRows.length) {
    const reliability = Array.from({ length: bins }, (_, index): ReliabilityBin => ({
      lower: index / bins, upper: (index + 1) / bins, count: 0, accuracy: null, mean_confidence: null,
    }));
    const buckets = reliability.map(() => ({ correct: 0, confidence: 0 }));
    let squaredError = 0;
    for (const row of confidenceRows) {
      const confidence = row.confidence!;
      // Released confidences are millionths; integer products preserve exact bin boundaries.
      const index = Math.min(bins - 1, Math.floor(Math.round(confidence * 1_000_000) * bins / 1_000_000));
      reliability[index]!.count++;
      buckets[index]!.correct += Number(row.correct);
      buckets[index]!.confidence += confidence;
      squaredError += (confidence - Number(row.correct)) ** 2;
    }
    let ece = 0;
    for (const [index, bin] of reliability.entries()) {
      if (!bin.count) continue;
      bin.accuracy = buckets[index]!.correct / bin.count;
      bin.mean_confidence = buckets[index]!.confidence / bin.count;
      ece += bin.count / confidenceRows.length * Math.abs(bin.accuracy - bin.mean_confidence);
    }
    calibration = {
      scope: 'caller_data_only', label: "Computed on the caller's data only.",
      method: 'selected_outcome_correctness', count: confidenceRows.length, bins: reliability,
      ece, brier_score: squaredError / confidenceRows.length,
    };
  }
  return {
    version: 1,
    items: {
      total: projected.length, completed: projected.filter(row => row.outcome === 'completed').length,
      refused: projected.filter(row => row.outcome === 'refused').length,
      held: projected.filter(row => row.outcome === 'held').length, error: projected.filter(row => row.outcome === 'error').length,
    },
    metrics: accuracy(questions),
    by_type: { boolean: accuracy(questions.filter(row => row.type === 'boolean')), choice: accuracy(questions.filter(row => row.type === 'choice')), score: accuracy(questions.filter(row => row.type === 'score')) },
    choice_confusion: choiceConfusion, latency_ms: { count: latency.length, p50: percentile(latency, 0.5), p95: percentile(latency, 0.95) },
    usage: { totals, records_with_usage: withUsage, records_without_usage: projected.length - withUsage },
    calibration, records: projected,
  };
}

export function reportJson(report: EvaluationReport): string {
  return asciiJson(report, true) + '\n';
}
function markdown(value: string | number | null): string {
  if (value === null) return 'unavailable';
  return String(value).replace(/[&<>|\\\x60*[\]\r\n\u0080-\uffff]/g, character => '&#' + character.charCodeAt(0) + ';');
}
export function reportMarkdown(report: EvaluationReport): string {
  const lines = [
    '# Batch evaluation ', '',
    '| Items | Completed | Refused | Held | Errors |', '| --- | --- | --- | --- | --- |',
    `| ${report.items.total} | ${report.items.completed} | ${report.items.refused} | ${report.items.held} | ${report.items.error} |`, '',
    '| Type | Questions | Correct | Accuracy | False-refusal rate |', '| --- | --- | --- | --- | --- |',
    ...types.map(type => { const row = report.by_type[type]; return `| ${type} | ${row.questions} | ${row.correct} | ${markdown(row.accuracy)} | ${markdown(row.false_refusal_rate)} |`; }), '',
    `Accuracy: ${markdown(report.metrics.accuracy)}; false-refusal rate: ${markdown(report.metrics.false_refusal_rate)}.`, '',
    `Latency ms (linear interpolation): p50 ${markdown(report.latency_ms.p50)}, p95 ${markdown(report.latency_ms.p95)}; samples ${report.latency_ms.count}.`, '',
    `Usage supplied by ${report.usage.records_with_usage} items; unavailable for ${report.usage.records_without_usage} items.`, '',
    '| Usage field | Total |', '| --- | --- |', ...usageFields.map(key => `| ${key} | ${report.usage.totals[key]} |`), '',
  ];
  if (report.calibration) {
    const calibration = report.calibration;
    lines.push(calibration.label, '', `Selected-outcome correctness: ECE ${calibration.ece}; Brier score ${calibration.brier_score}; samples ${calibration.count}.`, '',
      '| Lower | Upper | Count | Accuracy | Mean confidence |', '| --- | --- | --- | --- | --- |',
      ...calibration.bins.map(bin => `| ${bin.lower} | ${bin.upper} | ${bin.count} | ${markdown(bin.accuracy)} | ${markdown(bin.mean_confidence)} |`), '');
  }
  lines.push('| Question ID | Choice schema | Expected index | Actual index | Count |', '| --- | --- | --- | --- | --- |');
  for (const group of report.choice_confusion) for (const cell of group.counts) lines.push(`| ${markdown(group.question_id)} | ${group.choice_schema} | ${cell.expected} | ${cell.actual} | ${cell.count} |`);
  lines.push('', '| Item ID | Outcome | Code | Latency ms |', '| --- | --- | --- | --- |');
  for (const row of report.records) lines.push(`| ${markdown(row.id)} | ${row.outcome} | ${markdown(row.code)} | ${row.latency_ms} |`);
  return lines.join('\n') + '\n';
}
