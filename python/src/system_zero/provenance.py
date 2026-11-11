"""Metadata identifying the decision and the release that answered it."""

from typing import Dict, List, Literal, Mapping, Optional, TypeAlias, TypedDict, cast


ProvenanceAttachmentClass: TypeAlias = Literal["document", "image", "audio", "video"]
ProvenanceRoute: TypeAlias = Literal["direct", "long"]


class DecisionProvenance(TypedDict):
    """Original decision metadata; numeric_fingerprint is 64 lowercase hex."""

    decision_id: str
    request_id: str
    created: int
    model: str
    resolved_release: str
    render_id: str
    numeric_fingerprint: str
    calibration_version: Dict[str, str]
    route: ProvenanceRoute
    served_by: str
    attachment_classes: List[ProvenanceAttachmentClass]
    keyed_replay: bool


class ProvenanceReplyFields(TypedDict, total=False):
    """Absent on the largest context class, on a reply too large to carry it and on decisions stored before 2026-11-11."""

    provenance: DecisionProvenance


def provenance_for(reply: Mapping[str, object]) -> Optional[DecisionProvenance]:
    """Return the supplied wire value, with no synthesis or mutation.

    request_id belongs to the original decision, not the replay's HTTP header or
    stream frame. model is requested; resolved_release is the validated release.
    calibration_version maps answered question names to their exact node values.
    """
    return cast(Optional[DecisionProvenance], reply.get("provenance"))
