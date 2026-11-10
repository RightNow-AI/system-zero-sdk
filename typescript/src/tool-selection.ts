import type { Question } from './api-types.js';
import type { RequestOptions } from './client.js';
import type { SystemZeroClient } from './client.js';
import { decisionFor } from './decisions.js';
import { APIError, InvalidRequestError, SystemZeroError } from './errors.js';

export type ToolDefinition = { name: string; description?: string; [key: string]: unknown }
  | { type: 'function'; function: { name: string; description?: string; [key: string]: unknown } };
export type ToolSelectionMode = 'auto' | 'required';
type WithoutQuestions<T> = T extends unknown ? Omit<T, 'questions'> : never;
export type ToolSelectionRequest = WithoutQuestions<Parameters<SystemZeroClient['decide']>[0]>;
export interface ToolSelectionResult {
  decision: 'call' | 'none';
  tool: string | null;
  confidence: number | null;
  alternatives: Array<{ tool: string; confidence: number }>;
  reply: Awaited<ReturnType<SystemZeroClient['decide']>> | null;
  forced: boolean;
}

function invalid(param: string): never {
  throw new InvalidRequestError('Invalid tool selection definition.', {
    status: 400, type: 'invalid_request_error', code: 'invalid_request', param, retryable: false,
  });
}
function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** Compile sz-tools-v1; only names and descriptions enter the question. */
export function toolQuestions(tools: readonly ToolDefinition[], mode: ToolSelectionMode = 'auto'): { sz_tool: Extract<Question, { type: 'choice' }> } {
  if (mode !== 'auto' && mode !== 'required') invalid('mode');
  if (!Array.isArray(tools) || tools.length === 0) invalid('tools');
  const entries: Array<[string, string | null]> = [];
  const names = new Set<string>();
  for (const [index, tool] of tools.entries()) {
    if (!record(tool)) invalid(`tools[${index}]`);
    const definition = Object.hasOwn(tool, 'function') ? tool.function : tool;
    if (!record(definition)) invalid(`tools[${index}]`);
    const { name, description } = definition;
    if (typeof name !== 'string' || !/^[A-Za-z0-9_-]{1,64}$/u.test(name)
      || /^\d+$/u.test(name) || name === 'none' || names.has(name)) invalid(`tools[${index}].name`);
    if (description !== undefined && (typeof description !== 'string' || [...description].length > 1024)) invalid(`tools[${index}].description`);
    names.add(name);
    entries.push([name, description ?? null]);
  }
  if (mode === 'auto') entries.push(['none', 'No listed tool should be called.']);
  return { sz_tool: {
    type: 'choice',
    instructions: 'Select the one tool to call next for this state. Select "none" when no listed tool should be called.',
    criteria: Object.fromEntries(entries),
  } };
}

function noDecision(cause?: SystemZeroError): APIError {
  return new APIError('No decision, possibly refused.', {
    status: cause?.status ?? 503, type: cause?.type ?? 'api_error',
    code: cause?.code ?? 'service_unavailable', retryable: false,
    request_id: cause?.request_id ?? null, idempotencyKey: cause?.idempotencyKey ?? null,
    ...(cause ? { headers: cause.headers, cause } : {}),
  });
}

/** Select through ordinary decide, using only the client's normal retry budget. */
export async function selectTool(
  client: SystemZeroClient, request: ToolSelectionRequest, tools: readonly ToolDefinition[],
  mode: ToolSelectionMode = 'auto', options: RequestOptions = {},
): Promise<ToolSelectionResult> {
  const questions = toolQuestions(tools, mode);
  const names = Object.keys(questions.sz_tool.criteria);
  if (mode === 'required' && names.length === 1) {
    return { decision: 'call', tool: names[0]!, confidence: null, alternatives: [], reply: null, forced: true };
  }
  let reply: Awaited<ReturnType<SystemZeroClient['decide']>>;
  try { reply = await client.decide({ ...request, questions }, options); }
  catch (error) {
    if (error instanceof SystemZeroError && error.status === 503) throw noDecision(error);
    throw error;
  }
  const answer = reply.answers?.sz_tool;
  if (!answer || !('type' in answer) || answer.type !== 'choice') throw noDecision();
  const result = decisionFor(answer);
  if ((result.kind !== 'v1' && result.kind !== 'legacy') || typeof result.decision !== 'string'
    || !names.includes(result.decision) || !('probabilities' in answer) || Object.keys(answer.probabilities).length !== names.length
    || Object.keys(answer.probabilities).some(name => !names.includes(name))) throw noDecision();
  return {
    decision: result.decision === 'none' ? 'none' : 'call', tool: result.decision === 'none' ? null : result.decision,
    confidence: result.confidence,
    alternatives: result.alternatives.map(item => ({ tool: String(item.decision), confidence: item.confidence })),
    reply, forced: false,
  };
}
