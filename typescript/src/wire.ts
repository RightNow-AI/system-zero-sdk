/** Internal package-selected protocol names. Not a public client option. */
export type WireName = 'system-zero' | 'rightnow-decision';

export const VERSION = '0.5.0';
export const DEFAULT_BASE_URL = 'https://api.rightnowai.co/v1';
export const DANGER_ZONE_HEADERS = ['SZ-Danger-Zone', 'RightNow-Danger-Zone'] as const;
const resourcePath = (resource: string) => '/system-zero/' + resource;
const decisionModelPath = (resource: string) => '/decision-model/' + resource;

const legacy = Object.freeze({
  model: 'system-zero',
  versionedModel: 'system-zero-3.0.0',
  paths: Object.freeze({
    decide: resourcePath('decide'), decisions: resourcePath('decisions'), trace: '/trace',
    sessions: resourcePath('sessions'), uploads: resourcePath('uploads'),
    models: '/models', openapi: '/openapi.json',
  }),
  dangerZoneHeader: DANGER_ZONE_HEADERS[0],
  dangerZoneResponseHeader: 'sz-danger-zone',
  codes: Object.freeze({ timeout: 'system_zero_timeout', unavailable: 'system_zero_unavailable', servingUnattested: 'system_zero_serving_unattested' }),
  userAgent: `system-zero-typescript/${VERSION} node/${process.versions.node}`,
  // Compatibility packages retain the legacy stream and webhook names.
  streamURL: 'wss://api.rightnowai.co/v1/system-zero/stream',
  streamProtocol: 'system-zero.stream.v1' as const,
  webhooksPath: resourcePath('webhooks'),
  capabilitiesHeader: 'X-SZ-Capabilities',
  idempotencyPrefix: 'sz-sdk-',
  traceObject: 'system_zero.decision_trace' as 'system_zero.decision_trace' | 'decision_model.decision_trace',
});

export const WIRES = Object.freeze({
  'system-zero': Object.freeze({ ...legacy, name: 'system-zero' as const }),
  // The RightNow decision model's names, live on the gateway since names PR 1 to 3 (2026-11-13): the route family
  // /v1/decision-model/*, the ids rightnow-decision and rightnow-decision-X.Y.Z, the RightNow-Danger-Zone header and its
  // rightnow-danger-zone answer, and the model_* codes. Stream and webhook names are live since PRs 5 and 6.
  'rightnow-decision': Object.freeze({
    ...legacy,
    name: 'rightnow-decision' as const,
    model: 'rightnow-decision',
    versionedModel: 'rightnow-decision-3.0.0',
    paths: Object.freeze({
      decide: decisionModelPath('decide'), decisions: decisionModelPath('decisions'), trace: '/trace',
      sessions: decisionModelPath('sessions'), uploads: decisionModelPath('uploads'),
      models: decisionModelPath('models'), openapi: decisionModelPath('openapi.json'),
    }),
    dangerZoneHeader: DANGER_ZONE_HEADERS[1],
    dangerZoneResponseHeader: 'rightnow-danger-zone',
    codes: Object.freeze({ timeout: 'model_timeout', unavailable: 'model_unavailable', servingUnattested: 'model_serving_unattested' }),
    userAgent: `rightnow-typescript/${VERSION} node/${process.versions.node}`,
    streamURL: 'wss://api.rightnowai.co/v1/decision-model/stream',
    streamProtocol: 'rightnow-decision.stream.v1' as const,
    webhooksPath: decisionModelPath('webhooks'),
    traceObject: 'decision_model.decision_trace' as 'system_zero.decision_trace' | 'decision_model.decision_trace',
  }),
});
export type Wire = typeof WIRES[WireName];
const clients = new WeakMap<object, Wire>();
export function setWire(client: object, name: WireName): void { clients.set(client, WIRES[name]); }
export function wireFor(client?: object): Wire { return client && clients.get(client) || WIRES['rightnow-decision']; }
