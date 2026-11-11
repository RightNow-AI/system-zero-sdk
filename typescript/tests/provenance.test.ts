import { TEST_STREAM_LIMITS } from './stream-limit-fixture';
import { EventEmitter } from 'node:events';
import { readFileSync } from 'node:fs';
import { describe, expect, expectTypeOf, it } from 'vitest';
import { SystemZeroClient, SystemZeroStreamClient, decisionsFor, provenanceFor } from '../src/index.js';
import type { DecisionProvenance, LegacyReplayReply, ProvenanceAttachmentClass, ProvenanceReplyFields, ProvenanceRoute, StreamDecision, SystemZeroReply, V1DecideReply } from '../src/index.js';

type Fixture = { name: string; replayed: boolean; request_id: string; reply: SystemZeroReply };
const fixtures: Fixture[] = JSON.parse(readFileSync(new URL('../../fixtures/provenance/cases.json', import.meta.url), 'utf8'));
const request = { model: 'system-zero', state: 'SYNTHETIC', questions: { ready: { type: 'boolean' as const, instructions: 'Ready?' } } };

function fixtureSocket(fixture: Fixture) {
  return class Socket extends EventEmitter {
    readyState = 0;
    protocol = 'system-zero.stream.v1';
    seq = 0;
    constructor() {
      super();
      queueMicrotask(() => {
        this.readyState = 1;
        this.emit('open');
        this.server({ type: 'hello', protocol: this.protocol, request_id: 'req_upgrade', rate_limit: null,
          limits: { ...TEST_STREAM_LIMITS, heartbeat_interval_ms: 30_000, max_duration_ms: 3_600_000 } });
      });
    }
    server(frame: Record<string, unknown>) {
      this.emit('message', Buffer.from(JSON.stringify({ stream_id: 'szws_' + '1'.repeat(32), seq: ++this.seq, ...frame })), false);
    }
    send(text: string) {
      const frame = JSON.parse(text) as { type: string; event_id: string };
      if (frame.type !== 'start') return;
      queueMicrotask(() => {
        this.server({ type: 'ack', event_id: frame.event_id, state_version: 1, state_bytes: 11,
          synthesized_body_bytes: 100, scheduled: true });
        this.server({ type: 'decision', state_version: 1, covered_event_ids: [frame.event_id], reply: fixture.reply,
          request_id: fixture.request_id, http_status: 200, server_timing: null, latency_ms: null,
          rate_limit: null, queue_ms: 0, decide_ms: 1 });
      });
    }
    close(code = 1000) { this.readyState = 3; this.emit('close', code, Buffer.from('SYNTHETIC')); }
    terminate() { this.close(1006); }
  };
}

describe('native provenance shared SDK contract', () => {
  it('types the complete metadata without claiming a per-answer evidence shape', () => {
    expectTypeOf<DecisionProvenance['decision_id']>().toEqualTypeOf<string>();
    expectTypeOf<DecisionProvenance['numeric_fingerprint']>().toEqualTypeOf<string>();
    expectTypeOf<DecisionProvenance['created']>().toEqualTypeOf<number>();
    expectTypeOf<DecisionProvenance['calibration_version']>().toEqualTypeOf<Record<string, string>>();
    expectTypeOf<DecisionProvenance['keyed_replay']>().toEqualTypeOf<boolean>();
    expectTypeOf<ProvenanceRoute>().toEqualTypeOf<'direct' | 'long'>();
    expectTypeOf<ProvenanceAttachmentClass>().toEqualTypeOf<'document' | 'image' | 'audio' | 'video'>();
    expectTypeOf<ProvenanceReplyFields['provenance']>().toEqualTypeOf<DecisionProvenance | undefined>();
    expectTypeOf<V1DecideReply['provenance']>().toEqualTypeOf<DecisionProvenance | undefined>();
    expectTypeOf<LegacyReplayReply['provenance']>().toEqualTypeOf<DecisionProvenance | undefined>();
    expectTypeOf<SystemZeroReply['provenance']>().toEqualTypeOf<DecisionProvenance | undefined>();
    expectTypeOf<StreamDecision['reply']['provenance']>().toEqualTypeOf<DecisionProvenance | undefined>();
  });

  it.each(fixtures)('$name: accessor preserves supplied metadata and never fills an absence', fixture => {
    const before = JSON.stringify(fixture.reply);
    expect(provenanceFor(fixture.reply)).toBe(fixture.reply.provenance);
    expect(JSON.stringify(fixture.reply)).toBe(before);
    const { provenance: _provenance, ...withoutProvenance } = fixture.reply;
    expect(decisionsFor(fixture.reply)).toEqual(decisionsFor(withoutProvenance));
    if (fixture.reply.provenance) {
      const provenance = provenanceFor(fixture.reply)!;
      expect(provenance).not.toHaveProperty('evidence');
      expect(provenance.decision_id).toBe(fixture.reply.id);
      expect(provenance.resolved_release).toBe(fixture.reply.model);
      expect(provenance.route).toBe(fixture.reply.context.route);
      expect(provenance.served_by).toBe(fixture.reply.served_by);
      expect(provenance.calibration_version).toEqual(Object.fromEntries(Object.entries(fixture.reply.answers)
        .filter(([, answer]) => !('error' in answer))
        .map(([name, answer]) => [name, 'calibration_version' in answer ? answer.calibration_version : undefined])));
      expect(provenance.attachment_classes).toEqual([...new Set(provenance.attachment_classes)].sort());
    } else {
      expect(fixture.reply).not.toHaveProperty('provenance');
    }
  });

  it.each(fixtures)('$name: the HTTP client retains wire provenance and original request identity', async fixture => {
    const wire = JSON.stringify(fixture.reply);
    const client = new SystemZeroClient({ apiKey: 'rnsk_synthetic', baseURL: 'https://sdk.example.com/v1', maxRetries: 0,
      fetch: async () => new Response(wire, { headers: {
        'content-type': 'application/json', 'x-request-id': fixture.request_id,
        ...(fixture.replayed ? { 'x-rightnow-idempotent-replay': 'true' } : {}),
      } }) });
    const result = await client.decide(request).withResponse();
    expect(result.data).toEqual(fixture.reply);
    expect(JSON.stringify(result.data)).toBe(wire);
    expect(result.metadata.requestId).toBe(fixture.request_id);
    expect(result.metadata.replayed).toBe(fixture.replayed);
    expect(provenanceFor(result.data)).toEqual(fixture.reply.provenance);
    if (fixture.reply.provenance?.keyed_replay) {
      expect(provenanceFor(result.data)?.request_id).not.toBe(result.metadata.requestId);
    }
  });

  it.each(fixtures)('$name: the stream client retains the same complete reply', async fixture => {
    const client = new SystemZeroStreamClient({ apiKey: 'rnsk_synthetic', streamURL: 'wss://sdk.example.com/v1/system-zero/stream',
      WebSocket: fixtureSocket(fixture), reconnectPolicy: 'never' });
    try {
      const ack = await client.start(request);
      const result = await client.waitForDecision(ack);
      expect(result.decision.reply).toEqual(fixture.reply);
      expect(result.decision.request_id).toBe(fixture.request_id);
      expect(provenanceFor(result.decision.reply)).toEqual(fixture.reply.provenance);
      expect(JSON.stringify(result.decision.reply)).toBe(JSON.stringify(fixture.reply));
    } finally {
      await client.close();
    }
  });
});
