import { Buffer } from 'node:buffer';
import type { Answer, AuthoritativeAnswer, DecideReply, DecideRequest, DecisionAlternative, LegacyAnswer, Question, QuestionRefusalData } from './api-types.js';

export type Alternative = DecisionAlternative;
export type AnswerOrRefusal = Answer | QuestionRefusalData | { error: { code: string; retryable: false } };
export type SystemZeroReply = Omit<DecideReply, 'answers'> & { answers: Record<string, AnswerOrRefusal> };

export interface DecisionSummary {
  kind: 'v1';
  decision: boolean | string;
  confidence: number;
  /** Alias for v1 confidence, the probability of the selected outcome. */
  probability: number;
  alternatives: Alternative[];
  calibrated: boolean;
  /** Gateway metadata is copied verbatim; optional fields stay absent unless supplied. */
  calibration_version: string;
  preview?: true;
}

/** Legacy replay confidence was a margin and cannot drive probability thresholds. */
export interface LegacyDecision {
  kind: 'legacy';
  decision: boolean | string;
  confidence: null;
  probability: null;
  alternatives: [];
  calibrated: null;
  raw: LegacyAnswer;
}

/** A refusal is a result, not a request-level exception, and has no decision. */
export class QuestionRefusal {
  readonly kind = 'refusal';
  readonly code: string;
  readonly retryable = false;
  readonly error: { code: string; retryable: false };
  constructor(error: { code: string; retryable: false }) {
    this.code = error.code;
    this.error = error;
  }
}

export type DecisionResult = DecisionSummary | LegacyDecision | QuestionRefusal;

const markers = ['decision', 'alternatives', 'calibrated'] as const;
const million = 1_000_000;
function invalid(detail: string): never { throw new TypeError(`Invalid decision answer: ${detail}`); }
function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
function probability(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1;
}
function units(value: unknown): number {
  if (!probability(value)) invalid('probability must be a finite number in [0, 1]');
  const integer = Math.round(value * million);
  if (value !== integer / million) invalid('probability must use released millionths');
  return integer;
}
function distribution(answer: Record<string, unknown>, canonical: boolean): Map<string, number> {
  if (!record(answer.probabilities) || Object.keys(answer.probabilities).length === 0) invalid('missing probabilities');
  const values = new Map<string, number>();
  for (const [key, value] of Object.entries(answer.probabilities)) {
    if (!probability(value)) invalid('probabilities must be finite numbers in [0, 1]');
    values.set(key, canonical ? units(value) : value);
  }
  // Add in UTF-8 key order, the reader's tie order, so a legacy float total never depends on property enumeration order.
  let total = 0;
  for (const key of [...values.keys()].sort((a, b) => Buffer.compare(Buffer.from(a), Buffer.from(b)))) total += values.get(key)!;
   
  if (canonical ? Math.abs(total - million) > 1 || (values.size === 1 && total !== million) : Math.abs(total - 1) > 0.000001) invalid('probabilities must sum to one');
  return values;
}
function scoreLevels(answer: Record<string, unknown>, values: Map<string, number>): string[] {
  const keys = [...values.keys()].sort((a, b) => Number(a) - Number(b));
  if (keys.some((key, index) => key !== String(index))) invalid('score level keys must be zero-based decimal indices');
  if (!record(answer.legend) || Object.keys(answer.legend).length !== keys.length || keys.some(key => typeof (answer.legend as Record<string, unknown>)[key] !== 'string')) invalid('score legend must contain every level');
  if (typeof answer.score !== 'number' || !Number.isFinite(answer.score)) invalid('missing expected score');
  return keys;
}
function rank(values: Map<string, number>, score: boolean): string[] {
  return [...values.keys()].sort((a, b) => values.get(b)! - values.get(a)! || (score ? Number(a) - Number(b) : Buffer.compare(Buffer.from(a), Buffer.from(b))));
}
/** Adjacent nonnegative IEEE probability, used only inside the [0, 1] bounds. */
function adjacent(value: number, increasing: boolean): number {
  // Two 32-bit words preserve IEEE carry/borrow without requiring an ES2020 target.
  const bytes = new DataView(new ArrayBuffer(8));
  bytes.setFloat64(0, value);
  const high = bytes.getUint32(0), low = bytes.getUint32(4);
  const step = increasing ? 1 : -1;
  bytes.setUint32(4, low + step);
  if (increasing ? low === 0xffffffff : low === 0) bytes.setUint32(0, high + step);
  return bytes.getFloat64(0);
}
function rawProbabilityBound(value: number, increasing: boolean): number {
  // Invert upstream.ts's near(p * million, round(p * million)), including its float rounding.
  let bound = Math.max(0, Math.min(1, (value + (increasing ? 1e-6 : -1e-6)) / million));
  while (Math.abs(bound * million - value) > 1e-6) bound = adjacent(bound, !increasing);
  while (increasing ? bound < 1 : bound > 0) {
    const next = adjacent(bound, increasing);
    if (Math.abs(next * million - value) > 1e-6) break;
    bound = next;
  }
  return bound;
}
// Shared with Python: one unit is an interval pop or a boundary midpoint step.
// Sized for about 25 ms per exhausting ten-level answer in local Python timing,
// and at least 3x the largest completed legitimate search (6,177 units).
// Exhaustion accepts only after the conservative envelope passes.
const scoreSearchBudget = 19_000;
function scoreWitness(score: number, minimum: number[], maximum: number[]): boolean {
  // Both gateway sums are monotone in each nonnegative raw probability. Narrow IEEE
  // intervals with those same predicates, then split only if rounding plateaus remain.
  // The caller already passed the conservative envelope. On exhaustion trust the
  // gateway: this is a consistency check, never authorization.
  let remaining = scoreSearchBudget;
  const total = (raw: number[], weighted: boolean, index = -1, value = 0): number =>
    raw.reduce((sum, probability, level) => sum + (level === index ? value : probability) * (weighted ? level : 1), 0);
  const matches = (raw: number[]): boolean => Math.abs(total(raw, false) - 1) <= 1e-6 && Math.abs(total(raw, true) - score) <= 1e-6;
  const boundary = (low: number, high: number, passes: (value: number) => boolean, first: boolean): number | undefined => {
    while (true) {
      if (remaining === 0) return undefined;
      remaining--;
      const middle = (low + high) / 2;
      if (middle === low || middle === high) return first ? high : low;
      if (passes(middle) === first) high = middle;
      else low = middle;
    }
  };
  const pending = [{ low: minimum, high: maximum }];
  while (pending.length > 0) {
    if (remaining === 0) return true;
    remaining--;
    const { low, high } = pending.pop()!;
    let possible = true;
    for (let pass = 0; pass < 4 && possible; pass++) {
      let changed = false;
      for (const weighted of [false, true]) {
        const target = weighted ? score : 1;
        if (total(low, weighted) - target > 1e-6 || target - total(high, weighted) > 1e-6) { possible = false; break; }
        for (let index = 0; index < low.length; index++) {
          const above = (value: number): boolean => target - total(high, weighted, index, value) <= 1e-6;
          if (!above(high[index]!)) { possible = false; break; }
          if (!above(low[index]!)) {
            const bound = boundary(low[index]!, high[index]!, above, true);
            if (bound === undefined) return true;
            low[index] = bound;
            changed = true;
          }
          const below = (value: number): boolean => total(low, weighted, index, value) - target <= 1e-6;
          if (!below(low[index]!)) { possible = false; break; }
          if (!below(high[index]!)) {
            const bound = boundary(low[index]!, high[index]!, below, false);
            if (bound === undefined) return true;
            high[index] = bound;
            changed = true;
          }
        }
        if (!possible) break;
      }
      if (!changed) break;
    }
    if (!possible) continue;
    if (matches(low) || matches(high)) return true;
    let split = -1, width = 0;
    for (let index = 0; index < low.length; index++) {
      const span = (high[index]! - low[index]!) * Math.max(1, index);
      if (span > width) { split = index; width = span; }
    }
    if (split < 0) continue;
    let middle = (low[split]! + high[split]!) / 2;
    if (middle === high[split]) middle = adjacent(middle, false);
    const leftHigh = [...high], rightLow = [...low];
    leftHigh[split] = middle;
    rightLow[split] = adjacent(middle, true);
    pending.push({ low: [...low], high: leftHigh }, { low: rightLow, high: [...high] });
  }
  return false;
}
function expectedScore(score: number, values: Map<string, number>): boolean {
  const levels = [...values].sort(([a], [b]) => Number(a) - Number(b)).map(([, value]) => value);
  const projected = levels.map(value => value / million);
  if (Math.abs(projected.reduce((sum, value) => sum + value, 0) - 1) <= 1e-6 &&
      Math.abs(projected.reduce((sum, value, index) => sum + value * index, 0) - score) <= 1e-6) return true;
  const expected = levels.reduce((sum, value, index) => sum + index * value, 0) / million;
  const increasing = score > expected;
  const raw = levels.map(value => rawProbabilityBound(value, increasing));
  const withinMassBound = (): boolean => {
    const total = raw.reduce((sum, value) => sum + value, 0);
    return increasing ? total - 1 <= 1e-6 : 1 - total <= 1e-6;
  };
  // decision-fields.ts preserves the raw score while projecting probabilities. Find an
  // attainable boundary candidate by moving the lowest levels first. Binary search keeps
  // the original mass predicate; the witness search below handles IEEE sum plateaus.
  for (let index = 0; index < raw.length && !withinMassBound(); index++) {
    const initial = raw[index]!;
    raw[index] = rawProbabilityBound(levels[index]!, !increasing);
    if (!withinMassBound()) continue;
    let low = Math.min(initial, raw[index]!), high = Math.max(initial, raw[index]!);
    while (true) {
      const middle = (low + high) / 2;
      if (middle === low || middle === high) break;
      raw[index] = middle;
      if (withinMassBound() === increasing) low = middle;
      else high = middle;
    }
    raw[index] = increasing ? low : high;
  }
  const endpoint = raw.reduce((sum, value, index) => sum + value * index, 0);
  if (Math.abs(raw.reduce((sum, value) => sum + value, 0) - 1) <= 1e-6 && Math.abs(score - endpoint) <= 1e-6) return true;
  // Reject distant misses before searching plateaus. Sequential mass error is O(n * eps),
  // its score effect is at most n times that, and weighted-sum error is O(n * n * eps).
  // Only replies inside this conservative gamma_n envelope can use the bounded
  // search's acceptance fallback; outside misses are always rejected.
  const gamma = levels.length * Number.EPSILON / (1 - levels.length * Number.EPSILON);
  if (Math.abs(score - endpoint) > 1e-6 + 8 * gamma * levels.length) return false;
  return scoreWitness(score, levels.map(value => rawProbabilityBound(value, false)), levels.map(value => rawProbabilityBound(value, true)));
}
function alternatives(answer: Record<string, unknown>, expected: Alternative[]): void {
  if (!Array.isArray(answer.alternatives) || answer.alternatives.length !== expected.length) invalid('alternatives must contain every other outcome');
  for (const [index, value] of answer.alternatives.entries()) {
    const wanted = expected[index]!;
    if (!record(value) || Object.keys(value).length !== 2 || !Object.hasOwn(value, 'decision') || !Object.hasOwn(value, 'confidence')) invalid('alternatives require strict {decision, confidence} objects');
    if (value.decision !== wanted.decision || !probability(value.confidence) || value.confidence !== wanted.confidence) invalid('alternatives must match complete outcomes in probability and tie order');
  }
}

/** Validate the complete v1 field set; a confidence number alone is legacy. */
export function decisionFor(answer: AnswerOrRefusal, _question?: Question): DecisionResult {
  if (!record(answer)) invalid('answer must be an object');
  const source = answer as Record<string, unknown>;
  if (Object.hasOwn(source, 'error')) {
    const error = source.error;
    if (Object.keys(source).length !== 1 || !record(error) || Object.keys(error).length !== 2 || typeof error.code !== 'string' || error.retryable !== false) invalid('question refusal shape');
    return new QuestionRefusal({ code: error.code, retryable: false });
  }
  if (!['boolean', 'choice', 'score'].includes(String(source.type))) invalid('answer type');
  if (!probability(source.confidence) || typeof source.calibration_version !== 'string' || source.calibration_version.length < 1 || source.calibration_version.length > 200) invalid('confidence or calibration_version');
  const present = markers.filter(field => Object.hasOwn(source, field)).length;
  if (present !== 0 && present !== markers.length) invalid('incomplete v1 fields');
  const v1 = present === markers.length;
  if (v1 && typeof source.calibrated !== 'boolean') invalid('calibrated must be boolean');
  let selected: boolean | string;
  let expected: Alternative[] = [];
  let confidence: number;
  if (source.type === 'boolean') {
    if (!probability(source.probability)) invalid('boolean probability');
    const amount = v1 ? units(source.probability) : source.probability;
    const whole = v1 ? million : 1;
    selected = amount >= whole / 2;
    confidence = Math.max(amount, whole - amount) / whole;
    expected = [{ decision: !selected, confidence: Math.min(amount, whole - amount) / whole }];
  } else {
    const values = distribution(source, v1);
    const score = source.type === 'score';
    if (score && v1) scoreLevels(source, values);
    const ordered = rank(values, score);
    if (score) {
      selected = ordered[0]!;
      if (v1 && !expectedScore(source.score as number, values)) invalid('score must match the expected level index within the gateway tolerance');
    } else {
      if (typeof source.choice !== 'string' || !values.has(source.choice) || values.get(source.choice) !== values.get(ordered[0]!)) invalid('choice must be a selected maximum');
      selected = source.choice;
    }
    confidence = values.get(selected)! / (v1 ? million : 1);
    expected = ordered.filter(key => key !== selected).map(decision => ({ decision, confidence: values.get(decision)! / (v1 ? million : 1) }));
  }
  if (!v1) {
    return { kind: 'legacy', decision: selected, confidence: null, probability: null, alternatives: [], calibrated: null, raw: answer as LegacyAnswer };
  }
  if (source.decision !== selected || source.confidence !== confidence) invalid('decision and confidence must match the selected outcome');
  alternatives(source, expected);
  const server = answer as AuthoritativeAnswer;
  return {
    kind: 'v1', decision: server.decision, confidence: server.confidence,
    probability: server.confidence, alternatives: server.alternatives, calibrated: server.calibrated,
    calibration_version: server.calibration_version,
    ...(Object.hasOwn(server, 'preview') ? { preview: server.preview! } : {}),
  };
}

/** Refusals can accompany either version; successful answers cannot mix versions. */
export function decisionsFor(reply: SystemZeroReply, request?: Pick<DecideRequest, 'questions'>): Record<string, DecisionResult> {
  if (!record(reply.answers)) invalid('reply answers must be an object');
  let version: 'legacy' | 'v1' | undefined;
  const entries = Object.entries(reply.answers).map(([name, answer]) => {
    const result = decisionFor(answer, request?.questions[name]);
    if (result.kind !== 'refusal') {
      if (version !== undefined && version !== result.kind) invalid('reply cannot mix successful answer versions');
      version = result.kind;
    }
    return [name, result] as const;
  });
  return Object.fromEntries(entries);
}
