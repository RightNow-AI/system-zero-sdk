import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';
import * as sdk from '../src/index.js';

const names = (text: string): string[] => text.trim().split(/\s+/u).sort();
const apiCodes = names(`
  attachment_too_large audio_not_available capacity_exhausted committee_not_available
  daily_dispatch_limit deliberate_not_available document_controls_not_available document_decode_failed
  document_not_available document_pictures_not_available document_too_large document_unsupported
  https_required idempotency_key_reuse image_not_available injection_suspected
  input_quarantined insufficient_credits invalid_criteria invalid_request
  long_context_daily_budget_exhausted long_context_not_available long_duration_unsupported long_evidence_incomplete long_media_guard_refused
  long_no_readable_window long_options_unsupported long_phrase_invalid long_question_type_unsupported
  long_question_unsupported long_recording_not_available long_state_unsupported long_too_many_phrases
  media_decode_failed media_too_large media_unpriced media_unsupported output_limit_exceeded
  payload_too_large question_not_grounded question_too_large questions_too_large
  rate_limit_exceeded request_refused service_unavailable staging_attempt_ceiling
  stream_limit_exceeded state_too_large state_too_large_unguarded system_zero_timeout system_zero_unavailable
  upload_in_use upload_not_found upload_not_ready video_not_available
  workspace_limit_exceeded workspace_quarantined
`);
const gatewayCodes = names(`
  auth_error account_suspended model_not_found unknown_endpoint method_not_allowed invalid_trace_header invalid_api_key invalid_idempotency_key
  idempotency_conflict idempotency_replay_unavailable idempotency_replayed_error idempotency_unavailable limiter_unavailable
  maintenance internal_error concurrency_limit_exceeded monthly_cap_reached
`);
const rootNames = names(`Attachment InlineAttachment MEDIA_LIMITS MEDIA_MIMES MediaInput MediaKind VIDEO_PROFILE attachmentMagicMatches inlineAttachment validateAttachments videoAttachment OrdinaryQuestion MediaQuestion LongContextQuestion OrdinaryRequest MediaRequest LongContextRequest EnabledDecideRequest LongContextLimits VideoRequest VideoAttachment
  STREAM_PROTOCOL STREAM_CLIENT_IMPLEMENTED STREAM_RECEIPT_TIMEOUT_MS StreamProtocolError StreamDisconnectedError SystemZeroStreamClient DecisionMode ReconnectPolicy StreamRequest StreamStartOptions StreamRateLimit StreamBase StreamHello StreamAck StreamScheduled StreamDecision StreamError StreamGoodbye PossiblyDecided StreamEvent EventLatency StreamWebSocket StreamWebSocketConstructor StreamClientOptions ClientMutation StreamLimitError
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
  './stream': ['stream.ts', names(`STREAM_PROTOCOL STREAM_CLIENT_IMPLEMENTED STREAM_RECEIPT_TIMEOUT_MS StreamProtocolError StreamDisconnectedError SystemZeroStreamClient DecisionMode ReconnectPolicy StreamRequest StreamStartOptions StreamRateLimit StreamBase StreamHello StreamAck StreamScheduled StreamDecision StreamError StreamGoodbye PossiblyDecided StreamEvent EventLatency StreamWebSocket StreamWebSocketConstructor StreamClientOptions ClientMutation`)],
  '.': ['index.ts', rootNames],
  './integrations/function-tools': ['integrations/function-tools/index.ts', names(`
    ToolResult ToolHandlerOptions ToolHandler NESTED_FUNCTION_TOOLS FLAT_FUNCTION_TOOLS INPUT_SCHEMA_TOOLS
    TOOL_DEFINITIONS ToolArgumentError validateToolArguments deterministicKey createToolHandler handleToolCall
  `)],
  './integrations/mcp-server': ['integrations/mcp-server/server.ts', names(`
    CURRENT_PROTOCOL_VERSION LEGACY_PROTOCOL_VERSION SUPPORTED_PROTOCOL_VERSIONS MAX_FRAME_BYTES
    RpcReply McpServerOptions McpServer createMcpServer StdioOptions serveStdio
  `)],
} as const;

describe('public package surface', () => {
  it('publishes exactly the API error conveniences', () => {
    expect(apiCodes).toHaveLength(57);
    expect(gatewayCodes).toHaveLength(17);
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
