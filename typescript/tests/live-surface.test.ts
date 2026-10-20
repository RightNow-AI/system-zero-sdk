import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';
import * as sdk from '../src/index.js';

const names = (text: string): string[] => text.trim().split(/\s+/u).sort();
const apiCodes = names(`
  invalid_request invalid_criteria injection_suspected input_quarantined workspace_quarantined
  request_refused question_too_large question_not_grounded questions_too_large state_too_large
  state_too_large_unguarded payload_too_large long_context_not_available long_recording_not_available
  video_not_available image_not_available audio_not_available document_not_available
  committee_not_available
  deliberate_not_available https_required rate_limit_exceeded workspace_limit_exceeded
  capacity_exhausted daily_dispatch_limit idempotency_key_reuse service_unavailable
  system_zero_unavailable system_zero_timeout
`);
const gatewayCodes = names(`
  auth_error account_suspended model_not_found unknown_endpoint method_not_allowed invalid_trace_header invalid_api_key invalid_idempotency_key
  idempotency_conflict idempotency_replay_unavailable idempotency_replayed_error idempotency_unavailable limiter_unavailable
  maintenance internal_error concurrency_limit_exceeded monthly_cap_reached
`);
const rootNames = names(`
  JsonValue DecideRequest Question StructuredJson V1DecideReply
  BooleanAnswer ChoiceAnswer ScoreAnswer AuthoritativeAnswer LegacyReplayReply
  LegacyBooleanAnswer LegacyChoiceAnswer LegacyScoreAnswer LegacyAnswer QuestionRefusalData
  ModelList Model ErrorEnvelope DecisionAlternative Answer DecideReplyFields LegacyDecideReply DecideReply
  VERSION DEFAULT_BASE_URL Fetch ClientOptions RequestOptions withRetryContext ResponseMetadata
  ResponseResult APIPromise SystemZeroClient Alternative AnswerOrRefusal SystemZeroReply DecisionSummary
  LegacyDecision QuestionRefusal DecisionResult decisionFor decisionsFor shouldRetry shouldRecoverWorkflow
  retryAfterMs retryWaitMs markResponseStarted FlowCondition FlowTemplate FlowStep FlowDefinition FlowOptions FlowAnswerTrace
  FlowErrorTrace FlowStepTrace FlowTrace FlowError flowRequestHash
  flowIdempotencyKey validateFlow runFlow BatchItem BatchRecord BatchOptions BatchResult requestHash idempotencyKey runBatch ExpectedAnswers QuestionType Usage QuestionScore
  EvaluationRecord AccuracyMetrics ReliabilityBin EvaluationReport validateExpected scoreRefusal scoreReply
  evaluate reportJson reportMarkdown SYSTEM_ZERO_ERROR_CODES GATEWAY_ERROR_CODES ERROR_CODES KnownErrorCode
  SystemZeroError InvalidRequestError AuthenticationError PermissionError NotFoundError RateLimitError
  OverloadedError APIError ServerError APIConnectionError APITimeoutError PayloadTooLargeError InvalidCriteriaError ServiceUnavailableError CapabilityNotAvailableError RouteNotAvailableError ImageNotAvailableError AudioNotAvailableError
  DocumentNotAvailableError VideoNotAvailableError LongContextNotAvailableError LongRecordingNotAvailableError CommitteeNotAvailableError DeliberateNotAvailableError
  ErrorFields ConnectionErrorFields responseError
`);
const entries = {
  '.': ['index.ts', rootNames],
  './integrations/function-tools': ['integrations/function-tools/index.ts', names(`
    ToolResult ToolHandlerOptions ToolHandler OPENAI_TOOLS OPENAI_RESPONSES_TOOLS ANTHROPIC_TOOLS
    TOOL_DEFINITIONS ToolArgumentError validateToolArguments deterministicKey createToolHandler handleToolCall
  `)],
  './integrations/mcp-server': ['integrations/mcp-server/server.ts', names(`
    CURRENT_PROTOCOL_VERSION LEGACY_PROTOCOL_VERSION SUPPORTED_PROTOCOL_VERSIONS MAX_FRAME_BYTES
    RpcReply McpServerOptions McpServer createMcpServer StdioOptions serveStdio
  `)],
} as const;

describe('public package surface', () => {
  it('publishes exactly the API error conveniences', () => {
    expect([...sdk.SYSTEM_ZERO_ERROR_CODES].sort()).toEqual(apiCodes);
    expect([...sdk.GATEWAY_ERROR_CODES].sort()).toEqual(gatewayCodes);
    expect(Object.values(sdk.ERROR_CODES).sort()).toEqual([...apiCodes, ...gatewayCodes].sort());
  });

  it('exports exactly the documented API types and client helpers', () => {
    const filenames = Object.values(entries).map(([file]) => fileURLToPath(new URL('../src/' + file, import.meta.url)));
    const program = ts.createProgram(filenames, {
      target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext,
      moduleResolution: ts.ModuleResolutionKind.Bundler, noEmit: true, skipLibCheck: true,
    });
    const checker = program.getTypeChecker();
    for (const [index, [subpath, [, expected]]] of Object.entries(entries).entries()) {
      const source = program.getSourceFile(filenames[index]!)!;
      const symbol = checker.getSymbolAtLocation(source);
      const symbols = symbol ? checker.getExportsOfModule(symbol) : [];
      expect(symbols.map(value => value.getName()).sort(), subpath).toEqual(expected);
      if (subpath === '.') {
        const runtime = symbols.filter(value => {
          const target = value.flags & ts.SymbolFlags.Alias ? checker.getAliasedSymbol(value) : value;
          return Boolean(target.flags & ts.SymbolFlags.Value);
        }).map(value => value.getName()).sort();
        expect(Object.keys(sdk).sort()).toEqual(runtime);
      }
    }
    const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));
    expect(Object.keys(pkg.exports).sort()).toEqual([...Object.keys(entries), './package.json'].sort());
    expect(pkg.exports['./package.json']).toBe('./package.json');
  });

  it('exposes only the HTTP client resources and metadata options', () => {
    const client = new sdk.SystemZeroClient({ apiKey: 'surface-test', maxRetries: 0 });
    expect(Object.keys(client).sort()).toEqual(['models', 'options']);
    expect(Object.keys(client.models)).toEqual(['list']);
    expect(Object.getOwnPropertyNames(Object.getPrototypeOf(client)).sort()).toEqual(['constructor', 'decide']);
  });
});
