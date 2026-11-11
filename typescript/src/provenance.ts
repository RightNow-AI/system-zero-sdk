/** Metadata identifying the decision and the release that answered it. */
export type ProvenanceAttachmentClass = 'document' | 'image' | 'audio' | 'video';
export type ProvenanceRoute = 'direct' | 'long';

export interface DecisionProvenance {
  decision_id: string;
  /** Original decision request id; a keyed replay has a new transport request id. */
  request_id: string;
  created: number;
  /** Model string the caller requested, including an alias when used. */
  model: string;
  /** Concrete release in the validated node reply. */
  resolved_release: string;
  render_id: string;
  /** The validated serving fingerprint, encoded as 64 lowercase hex characters. */
  numeric_fingerprint: string;
  /** Exact version by answered question; question refusals have no entry. */
  calibration_version: Record<string, string>;
  route: ProvenanceRoute;
  served_by: string;
  /** Sorted unique attachment kinds; no names, locations or content. */
  attachment_classes: ProvenanceAttachmentClass[];
  keyed_replay: boolean;
}

export interface ProvenanceReplyFields {
  /** Absent on the largest context class, on a reply too large to carry it and on decisions stored before 2026-11-11. */
  provenance?: DecisionProvenance;
}

/** Read the wire value without deriving, changing or filling any metadata. */
export function provenanceFor(reply: { id: string } & ProvenanceReplyFields): DecisionProvenance | undefined {
  return reply.provenance;
}
