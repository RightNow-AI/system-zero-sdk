import type { Attachment, DecideReply, DecideRequest, ErrorEnvelope, JsonValue, StructuredJson } from './api-types.js';

export type DecisionMode = 'on_change' | 'on_request';
export type ReconnectPolicy = 'latest' | 'never';
export type StreamRequest = DecideRequest;
export interface StreamStartOptions { decisionMode?: DecisionMode; minIntervalMs?: number }
export interface StreamRateLimit { limit: number; remaining: number; reset_epoch_seconds: number }
export interface StreamBase { stream_id: string; seq: number }
export interface StreamHello extends StreamBase {
  type: 'hello'; protocol: 'system-zero.stream.v1'; request_id: string;
  limits: Record<string, number>; rate_limit: StreamRateLimit | null;
}
export interface StreamAck extends StreamBase {
  type: 'ack'; event_id: string; state_version: number; state_bytes: number;
  synthesized_body_bytes: number; scheduled: boolean;
}
export interface StreamScheduled extends StreamBase { type: 'scheduled'; state_version: number; in_flight: boolean }
export interface StreamDecision extends StreamBase {
  type: 'decision'; state_version: number; covered_event_ids: string[];
  reply: DecideReply; request_id: string; http_status: 200; server_timing: string | null;
  latency_ms: number | null; rate_limit: StreamRateLimit | null; queue_ms: number; decide_ms: number;
}
export interface StreamError extends StreamBase {
  type: 'error'; phase: 'protocol' | 'decide' | 'lifecycle'; state_version: number;
  event_id: string | null; covered_event_ids: string[]; http_status: number; envelope: ErrorEnvelope;
  retry: { should_retry: boolean; retry_after_ms: number | null; retry_after: string | null };
  rate_limit: StreamRateLimit | null; server_timing: string | null; fatal: boolean;
}
export interface StreamGoodbye extends StreamBase {
  type: 'goodbye'; reason: 'shutdown' | 'max_lifetime' | 'idle' | 'maintenance' | 'lease_lost' | 'slow_reader' | 'store_unavailable' | 'overload' | 'auth' | 'policy' | null;
  state_version: number; last_decision_state_version: number;
  resume: { supported: false; action: 'reconnect_and_replay'; retry_after_ms: number };
}
/** Conservative scheduled/sent work whose completion cannot be known after a lost connection. */
export interface PossiblyDecided { type: 'possibly_decided'; stream_id: string; state_versions: number[]; event_ids: string[] }
export type StreamEvent = StreamAck | StreamScheduled | StreamDecision | StreamError | StreamGoodbye | PossiblyDecided;
export interface EventLatency { eventId: string; stateVersion: number; latencyMs: number; decision: StreamDecision }

/**
 * ws-compatible transports expose control pings through the public `ping` event.
 * EventTarget-only transports use bounded fallback timers because their public
 * API cannot report progress inside fragmented messages.
 */
export interface StreamWebSocket {
  readonly readyState: number; readonly protocol: string;
  send(data: string): void; pong?(data: Uint8Array): void; close(code?: number, reason?: string): void; terminate?(): void;
  on?(type: string, listener: (...args: any[]) => void): unknown;
  off?(type: string, listener: (...args: any[]) => void): unknown;
  removeListener?(type: string, listener: (...args: any[]) => void): unknown;
  addEventListener?(type: string, listener: (event: any) => void): void;
  removeEventListener?(type: string, listener: (event: any) => void): void;
}
export interface StreamWebSocketConstructor {
  new(url: string, protocols: string[], options: { headers: Record<string, string>; perMessageDeflate: false; autoPong?: false }): StreamWebSocket;
}
export interface StreamClientOptions {
  apiKey?: string; streamURL?: string; WebSocket?: StreamWebSocketConstructor;
  reconnectPolicy?: ReconnectPolicy;
  /** Total consecutive opening attempts, including the first. Defaults to 8. */
  maxReconnectAttempts?: number;
  /** Milliseconds for an opening episode, including retries and waits. Defaults to 120000. */
  reconnectTimeout?: number;
  /** Monotonic milliseconds; the default is performance.now(). */
  now?: () => number; random?: () => number;
  /**
   * Connection/hello and receipt inactivity watchdog, in milliseconds. Authenticated
   * heartbeats can extend receipts up to the fixed 3818000 ms bound, which covers
   * the gateway lifetime and final output flush. Hello hints cannot alter it.
   * EventTarget-only transports with a valid receipt hint use that fixed bound.
   * Their heartbeat deadline adds one advertised interval, capped at 30 seconds,
   * because a fragmented message can begin just before the next JSON heartbeat.
   */
  timeout?: number;
}
export type ClientMutation =
  ({ idempotency_key?: string } & (
  | { type: 'start'; event_id: string; request: StreamRequest; decision_mode: DecisionMode; min_interval_ms: number }
  | { type: 'append_text'; event_id: string; text: string }
  | { type: 'append_items'; event_id: string; items: JsonValue[] }
  | { type: 'append_attachments'; event_id: string; attachments: Attachment[] }
  | { type: 'replace'; event_id: string; state: StructuredJson; attachments?: Attachment[] }));
