import type {
  Answer, AuthoritativeAnswer, BooleanAnswer, ChoiceAnswer, DecideRequest,
  Question, QuestionRefusalData, ScoreAnswer, V1DecideReply,
} from './api-types.js';
import {
  decisionFor, type Alternative, type AnswerOrRefusal, type DecisionResult, type SystemZeroReply,
} from './decisions.js';
import {
  InvalidRequestError, PolicyConfidenceFailedError, PolicyMaxCostExceededError,
  responseError, SystemZeroError, type ErrorFields,
} from './errors.js';
export { PolicyConfidenceFailedError, PolicyMaxCostExceededError } from './errors.js';

/** Optional request policy. Omitted on_low_confidence means abstain on the gateway. */
export type Policy = NonNullable<DecideRequest['policy']>;

export type PolicyDecideRequest = Omit<DecideRequest, 'policy'> & { policy?: Policy };
export type PolicyStreamRequest = Omit<PolicyDecideRequest, 'session_id' | 'attachments'> & {
  session_id?: never; attachments?: NonNullable<DecideRequest['attachments']>;
};
export type AbstentionReason = 'low_confidence' | 'uncalibrated';
type Abstained<T> = Omit<T, 'decision'> & {
  decision?: never;
  abstained: true;
  abstention_reason: AbstentionReason;
};
export type AbstainedBooleanAnswer = Abstained<BooleanAnswer>;
export type AbstainedChoiceAnswer = Abstained<ChoiceAnswer>;
export type AbstainedScoreAnswer = Abstained<ScoreAnswer>;
export type AbstainedAnswer = AbstainedBooleanAnswer | AbstainedChoiceAnswer | AbstainedScoreAnswer;
export type PolicyAnswer = Answer | AbstainedAnswer;
export type PolicyAnswerOrRefusal = AnswerOrRefusal | AbstainedAnswer;
export type PolicyV1DecideReply = Omit<V1DecideReply, 'answers' | 'policy'> & {
  answers: Record<string, AuthoritativeAnswer | AbstainedAnswer | QuestionRefusalData>;
  policy?: Policy;
};
export type PolicyReply = Omit<SystemZeroReply, 'answers' | 'policy'> & {
  answers: Record<string, PolicyAnswerOrRefusal>;
  policy?: Policy;
};
export type PolicyDecideReplyFields = Omit<PolicyReply, 'answers'>;

/** A completed answer charged normally and counted toward monthly usage, with its decision withheld. */
export interface PolicyAbstention {
  kind: 'abstained';
  abstained: true;
  abstention_reason: AbstentionReason;
  confidence: number;
  probability: number;
  alternatives: Alternative[];
  calibrated: boolean;
  calibration_version: string;
  preview?: true;
  raw: AbstainedAnswer;
}

export type PolicyDecisionResult = DecisionResult | PolicyAbstention;

const policyMembers = new Set(['min_confidence', 'on_low_confidence', 'require_calibrated', 'max_cost_usd']);

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function invalidPolicy(param: string, detail: string): never {
  throw new InvalidRequestError(`Invalid policy: ${detail}`, {
    status: 400, type: 'invalid_request_error', code: 'invalid_request', param, retryable: false,
  });
}

/** Exact microcents for the canonical decimal number that this SDK serializes. */
export function policyMaxCostMicrocents(value: unknown): number {
  const invalid = (): never => invalidPolicy('policy.max_cost_usd', 'max_cost_usd must be a finite number greater than zero, at most 1000, with at most eight fractional digits.');
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0 || value > 1000) return invalid();
  const [mantissa = '', exponentText = '0'] = value.toString().split('e');
  const point = mantissa.indexOf('.');
  const fractionalDigits = point < 0 ? 0 : mantissa.length - point - 1;
  const shift = 8 + Number(exponentText) - fractionalDigits;
  let microcents = BigInt(mantissa.replace('.', ''));
  if (shift >= 0) {
    microcents *= BigInt(10) ** BigInt(shift);
  } else {
    const divisor = BigInt(10) ** BigInt(-shift);
    if (microcents % divisor !== BigInt(0)) invalid();
    microcents /= divisor;
  }
  if (microcents < BigInt(1) || microcents > BigInt(100000000000)) invalid();
  return Number(microcents);
}

/** Validate policy only; preserve explicit members and leave defaults to the gateway. */
export function validatePolicy(value: unknown): Policy {
  if (!record(value)) invalidPolicy('policy', 'policy must be an object.');
  for (const field of Object.keys(value)) {
    if (!policyMembers.has(field)) invalidPolicy('policy.*', 'unknown member.');
  }
  if (Object.hasOwn(value, 'min_confidence')) {
    const confidence = value.min_confidence;
    if (typeof confidence !== 'number' || !Number.isFinite(confidence) || confidence <= 0 || confidence > 1) {
      invalidPolicy('policy.min_confidence', 'min_confidence must be a finite number greater than zero and at most one.');
    }
  }
  if (Object.hasOwn(value, 'on_low_confidence') && value.on_low_confidence !== 'abstain') {
    invalidPolicy('policy.on_low_confidence', 'Only abstain is available; error mode is held.');
  }
  if (Object.hasOwn(value, 'require_calibrated')) {
    invalidPolicy('policy.require_calibrated', 'require_calibrated is held until calibration is available.');
  }
  if (Object.hasOwn(value, 'max_cost_usd')) policyMaxCostMicrocents(value.max_cost_usd);
  return { ...value } as Policy;
}

/** The integration calls this before HTTP serialization or stream state capture. */
export function validateRequestPolicy<T>(request: T): T {
  if (record(request) && Object.hasOwn(request, 'policy')) validatePolicy(request.policy);
  return request;
}

function invalidAnswer(detail: string): never { throw new TypeError(`Invalid decision answer: ${detail}`); }

function answerFields(source: Record<string, unknown>): Extract<DecisionResult, { kind: 'v1' }> {
  // Restore a selection only in an internal validation copy. The ordinary reader
  // owns C's canonical fields and bounded score witness; an abstention never
  // exposes this selection or applies a client-side policy decision.
  let selected: unknown;
  if (source.type === 'boolean') selected = typeof source.probability === 'number' && source.probability >= 0.5;
  else if (source.type === 'choice') selected = source.choice;
  else if (source.type === 'score' && record(source.probabilities)) {
    let maximum = -Infinity;
    for (const [level, value] of Object.entries(source.probabilities)) {
      if (typeof value !== 'number') invalidAnswer('probabilities must be finite numbers in [0, 1]');
      if (selected === undefined || value > maximum || (value === maximum && Number(level) < Number(selected))) {
        selected = level;
        maximum = value;
      }
    }
  }
  const { abstained: _abstained, abstention_reason: _reason, ...ordinary } = source;
  const checked = decisionFor({ ...ordinary, decision: selected } as unknown as AnswerOrRefusal);
  if (checked.kind !== 'v1') invalidAnswer('an abstention requires complete v1 fields');
  return checked;
}

/** Read passed or abstained gateway evidence without applying client-side policy. */
export function policyDecisionFor(answer: PolicyAnswerOrRefusal, question?: Question): PolicyDecisionResult {
  if (!record(answer)) return decisionFor(answer as AnswerOrRefusal, question);
  const source = answer as Record<string, unknown>;
  if (!Object.hasOwn(source, 'abstained') && !Object.hasOwn(source, 'abstention_reason')) {
    return decisionFor(answer as AnswerOrRefusal, question);
  }
  if (source.abstained !== true || (source.abstention_reason !== 'low_confidence' && source.abstention_reason !== 'uncalibrated')) {
    invalidAnswer('abstained and abstention_reason must form a complete policy result');
  }
  if (Object.hasOwn(source, 'decision') || Object.hasOwn(source, 'error')) invalidAnswer('an abstention cannot contain a decision or refusal');
  const raw = answer as AbstainedAnswer;
  const checked = answerFields(source);
  if (source.abstention_reason === 'uncalibrated' && checked.calibrated) invalidAnswer('uncalibrated abstention contradicts calibrated=true');
  return {
    kind: 'abstained', abstained: true, abstention_reason: source.abstention_reason,
    confidence: checked.confidence, probability: checked.probability,
    alternatives: checked.alternatives, calibrated: checked.calibrated, raw,
    calibration_version: checked.calibration_version,
    ...(Object.hasOwn(checked, 'preview') ? { preview: checked.preview! } : {}),
  };
}

/** Abstentions count as v1; refusals remain valid beside either reply version. */
export function policyDecisionsFor(reply: PolicyReply, request?: Pick<DecideRequest, 'questions'>): Record<string, PolicyDecisionResult> {
  if (!record(reply.answers)) invalidAnswer('reply answers must be an object');
  let version: 'legacy' | 'v1' | undefined;
  const entries = Object.entries(reply.answers).map(([name, answer]) => {
    const result = policyDecisionFor(answer, request?.questions[name]);
    if (result.kind !== 'refusal') {
      const current = result.kind === 'abstained' ? 'v1' : result.kind;
      if (version !== undefined && version !== current) invalidAnswer('reply cannot mix successful answer versions');
      version = current;
    }
    return [name, result] as const;
  });
  return Object.fromEntries(entries);
}

export const POLICY_ERROR_CODES = ['policy_max_cost_exceeded', 'policy_confidence_failed'] as const;
export type PolicyErrorCode = typeof POLICY_ERROR_CODES[number];

export type PolicyError = PolicyMaxCostExceededError | PolicyConfidenceFailedError;

export const POLICY_ERROR_CLASSES = {
  policy_max_cost_exceeded: PolicyMaxCostExceededError,
  policy_confidence_failed: PolicyConfidenceFailedError,
} as const;

export function isPolicyError(error: unknown): error is PolicyError {
  return error instanceof PolicyMaxCostExceededError || error instanceof PolicyConfidenceFailedError;
}

/** Specialize an existing HTTP or stream error without changing its envelope. */
export function asPolicyError(error: SystemZeroError): PolicyError | undefined {
  if (isPolicyError(error)) return error;
  if (typeof error.code !== 'string' || !Object.hasOwn(POLICY_ERROR_CLASSES, error.code)) return undefined;
  const ErrorClass = POLICY_ERROR_CLASSES[error.code as PolicyErrorCode];
  const fields: ErrorFields = {
    status: error.status, type: error.type, code: error.code, param: error.param,
    retryable: error.retryable, request_id: error.request_id, headers: error.headers,
  };
  if (error.details !== undefined) fields.details = error.details;
  if (error.cause !== undefined) fields.cause = error.cause;
  return new ErrorClass(error.message, fields);
}

/** Drop-in responseError wiring avoids a cycle in the shared errors module. */
export function policyResponseError(status: number, headers: Headers, body: unknown, fallbackRetryable: boolean): SystemZeroError {
  const error = responseError(status, headers, body, fallbackRetryable);
  return asPolicyError(error) ?? error;
}
