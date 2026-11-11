// Request and response types for published API surfaces.


import type { ProvenanceReplyFields } from './provenance.js';

export type JsonValue = string | number | boolean | null | { [key: string]: JsonValue } | JsonValue[];

export type DecideRequest = ({ "model": string; "state": StructuredJson; "attachments"?: (Array<(Attachment) & ({ "type"?: "image" | "audio" | "document"; })>) | (Array<(Attachment) & ({ "type"?: "video"; "data"?: unknown; })>) | (null); "questions": Record<string, Question>; "session_id"?: string; "prompt_cache_key"?: string; "route"?: "standard"; "mode"?: "standard"; "policy"?: { "min_confidence"?: number; "on_low_confidence"?: "abstain"; "max_cost_usd"?: number; }; }) & (unknown);

export type Question = ({ "type": "boolean"; "instructions": StructuredJson; "criteria"?: { "true"?: string; "false"?: string; }; }) | ({ "type": "choice"; "instructions": StructuredJson; "criteria": Record<string, string | null>; }) | ({ "type": "score"; "instructions": StructuredJson; "criteria": Array<string>; });

export type Attachment = { "name": string; "type": "image" | "audio" | "document" | "video"; "data": string; };

export type StructuredJson = (string) | (Record<string, JsonValue>) | (Array<JsonValue>);

export type V1DecideReply = { "id": string; "model": string; "served_by": string; "created": number; "context": { "route": "direct"; "tokens": number; }; "answers": Record<string, (AuthoritativeAnswer) | (QuestionRefusalData) | (WireAbstainedAnswer)>; "usage": { "input_tokens": number; "cached_input_tokens": number; "media_tokens": number; "long_input_tokens": number; "media_items": number; "thought_tokens": number; }; "policy"?: { "min_confidence"?: number; "on_low_confidence"?: "abstain"; "max_cost_usd"?: number; }; } & ProvenanceReplyFields;

export type BooleanAnswer = { "type": "boolean"; "probability": number; "confidence": number; "calibration_version": string; "preview"?: true; "decision": boolean; "alternatives": Array<{ "decision": boolean; "confidence": number; }>; "calibrated": boolean; };

export type ChoiceAnswer = { "type": "choice"; "choice": string; "probabilities": Record<string, number>; "confidence": number; "calibration_version": string; "preview"?: true; "decision": string; "alternatives": Array<{ "decision": string; "confidence": number; }>; "calibrated": boolean; };

export type ScoreAnswer = { "type": "score"; "score": number; "legend": Record<string, string>; "probabilities": Record<string, number>; "confidence": number; "calibration_version": string; "preview"?: true; "decision": string; "alternatives": Array<{ "decision": string; "confidence": number; }>; "calibrated": boolean; };

export type AuthoritativeAnswer = BooleanAnswer | ChoiceAnswer | ScoreAnswer;

export type WireAbstainedAnswer = ({ "type": "boolean"; "probability": number; "confidence": number; "calibration_version": string; "preview"?: true; "alternatives": Array<{ "decision": boolean; "confidence": number; }>; "calibrated": boolean; "abstained": true; "abstention_reason": "low_confidence" | "uncalibrated"; }) | ({ "type": "choice"; "choice": string; "probabilities": Record<string, number>; "confidence": number; "calibration_version": string; "preview"?: true; "alternatives": Array<{ "decision": string; "confidence": number; }>; "calibrated": boolean; "abstained": true; "abstention_reason": "low_confidence" | "uncalibrated"; }) | ({ "type": "score"; "score": number; "legend": Record<string, string>; "probabilities": Record<string, number>; "confidence": number; "calibration_version": string; "preview"?: true; "alternatives": Array<{ "decision": string; "confidence": number; }>; "calibrated": boolean; "abstained": true; "abstention_reason": "low_confidence" | "uncalibrated"; });

export type LegacyReplayReply = { "id": string; "model": string; "served_by": string; "created": number; "context": { "route": "direct"; "tokens": number; }; "answers": Record<string, (LegacyAnswer) | (QuestionRefusalData)>; "usage": { "input_tokens": number; "cached_input_tokens": number; "media_tokens": number; "long_input_tokens": number; "media_items": number; "thought_tokens": number; }; } & ProvenanceReplyFields;

export type LegacyBooleanAnswer = { "type": "boolean"; "probability": number; "confidence": number; "calibration_version": string; "preview"?: true; };

export type LegacyChoiceAnswer = { "type": "choice"; "choice": string; "probabilities": Record<string, number>; "confidence": number; "calibration_version": string; "preview"?: true; };

export type LegacyScoreAnswer = { "type": "score"; "score": number; "legend": Record<string, string>; "probabilities": Record<string, number>; "confidence": number; "calibration_version": string; "preview"?: true; };

export type LegacyAnswer = LegacyBooleanAnswer | LegacyChoiceAnswer | LegacyScoreAnswer;

export type QuestionRefusalData = { "error": { "code": "injection_suspected" | "question_not_grounded"; "retryable": false; }; };

export type ModelList = { "object": "list"; "data": Array<Model>; };

export type Model = { "id": string; "object": "model"; "alias_for": string | null; "input_routes"?: { "audio"?: string; "document"?: string; "image"?: string; "video"?: string; }; "released_at": string | null; "calibration_versions": Array<string> | null; "limits": { "state_tokens": number; "rendered_tokens": number; "long_context"?: Array<{ "header_value": string; "min_state_tokens": number; "state_tokens": number; "rendered_tokens": number; "attachments": 0; }>; "questions": number; "question_tokens": number; "question_bytes": number; "choice_options": { "text": { "min": number; "max": number; }; "media": { "min": number; "max": number; }; "long_context"?: { "min": number; "max": number; }; }; "score_levels": { "text": { "min": number; "max": number; }; "media": { "min": number; "max": number; }; }; "attachments": number; "images"?: number; "audio"?: number; "body_bytes": number; "inline_attachment_bytes": number; }; "held_routes": Array<string>; };

export type ErrorEnvelope = { "error": { "message": string; "type": "invalid_request_error" | "authentication_error" | "permission_error" | "not_found_error" | "rate_limit_error" | "overloaded_error" | "api_error" | "server_error"; "param": string | null; "code": string | null; "retryable": boolean; "details"?: Record<string, JsonValue>; }; "request_id": string; };

export type DecisionId = string;

export type DecisionTrace = { "object": "system_zero.decision_trace"; "id": DecisionId; "request_id": string | null; "created": number; "model": string; "model_version": string; "render_id": string | null; "route": "direct" | "long"; "attachments": Array<{ "class": "document" | "image" | "audio" | "video"; "count": number; }> | null; "timings": { "queue_ms": number | null; "node_ms": number | null; "settle_ms": number | null; }; "usage_units": { "input_tokens": number | null; "cached_input_tokens": number | null; "media_tokens": number | null; "cached_media_tokens": number | null; "long_input_tokens": number | null; "media_items": number | null; "thought_tokens": number | null; }; "refusal_code": "injection_suspected" | "question_not_grounded" | "long_question_unsupported" | "long_options_unsupported" | "long_phrase_invalid" | "long_question_type_unsupported" | "long_media_guard_refused" | "long_no_readable_window" | "long_evidence_incomplete" | null; "refusal_codes": Array<"injection_suspected" | "question_not_grounded" | "long_question_unsupported" | "long_options_unsupported" | "long_phrase_invalid" | "long_question_type_unsupported" | "long_media_guard_refused" | "long_no_readable_window" | "long_evidence_incomplete">; "idempotency": "fresh" | "replay" | null; "stream_id": string | null; "state_version": number | null; };

export type DecisionAlternative<Decision extends boolean | string = boolean | string> = { decision: Decision; confidence: BooleanAnswer["confidence"]; };

export type Answer = AuthoritativeAnswer | LegacyAnswer | import('./policy.js').AbstainedAnswer;

export type DecideReplyFields = Omit<V1DecideReply, "answers">;

export type LegacyDecideReply = LegacyReplayReply;

export type DecideReply = V1DecideReply | LegacyReplayReply;
