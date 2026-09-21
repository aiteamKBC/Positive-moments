"""
The legacy-compatible session and checklist shapes.

Field mappings are taken from the `Upsert QA Session` and
`Upsert QA Checklist Items` nodes, and the value derivations from
`Build Session and Checklist Rows`. This module produces the SHADOW
representation only; writing it to a legacy table is Phase 3C's decision.

Two places where the platform deliberately does not reproduce legacy:

- Item 2 keeps the corrected, timezone-aware status. The historical legacy
  status is carried separately as a parity field, never merged into it.
- the trainer is the deterministic Phase 2C3 speaker, not
  `aiTrainer || lecturerFromVtt || lectureRow.trainer`.
"""
import hashlib
from datetime import timezone

from app.qa.checklist import MET, NOT_MET, PARTIALLY_MET


# Legacy severity mapping from `Build Session and Checklist Rows`.
SEVERITY = {MET: "pass", PARTIALLY_MET: "warning", NOT_MET: "fail"}


def session_id_match(session_id, checklist_order: int) -> str:
    """Legacy `session_id + "_" + checklist_order`, the checklist upsert key."""
    return f"{session_id}_{checklist_order}"


def legacy_date(actual_start) -> str:
    """
    Legacy `String(baseSession.createdDateTime).slice(0, 10)`.

    `createdDateTime` was the selected transcript's own start, a Graph UTC ISO
    string, so the legacy date is its UTC calendar date - not the Cairo
    business date. Reproduced here for compatibility; the canonical
    `session_date` is carried alongside it and any difference is reported.
    """
    if actual_start is None:
        return ""
    # Graph's createdDateTime is a UTC ISO string, so the slice is a UTC date.
    # Pinned explicitly rather than trusting the connection's timezone.
    moment = (actual_start if actual_start.tzinfo is not None
              else actual_start.replace(tzinfo=timezone.utc))
    return moment.astimezone(timezone.utc).date().isoformat()


def counts_from_statuses(statuses) -> dict:
    """
    met / partial / not-met over the FINAL statuses.

    Legacy counted the checklist after its Item 7 override was applied, so the
    counts must come from the final rows, never from the raw AI answers.
    """
    values = list(statuses)
    return {"met_count": values.count(MET),
            "partial_count": values.count(PARTIALLY_MET),
            "not_met_count": values.count(NOT_MET)}


def titled_items_to_object(entries, prefix: str, render_position) -> dict:
    """
    Legacy `titledItemsToObject`: strength_1, strength_2, area_1, ...

    `render_position(index)` renders the stored evidence clips recorded for
    that 1-based entry position - the same position Phase 3A used when it
    collected them. AI titles are passed through untouched.
    """
    rendered = {}
    for index, entry in enumerate(entries or [], start=1):
        entry = entry if isinstance(entry, dict) else {}
        evidence = render_position(index)
        rendered[f"{prefix}_{index}"] = {
            "title": entry.get("title") or "",
            "evidence": evidence["text"],
            "evidence_clips": entry.get("evidence_clips") or [],
            "cue_ids": [str(cue_id) for cue_id in evidence["cue_ids"]],
        }
    return rendered


def ksbs_to_object(entries, render_position) -> dict:
    """Legacy `ksbsToObject`: ksb_1, ksb_2, ... each with type, title, evidence."""
    rendered = {}
    for index, entry in enumerate(entries or [], start=1):
        entry = entry if isinstance(entry, dict) else {}
        evidence = render_position(index)
        rendered[f"ksb_{index}"] = {
            "type": entry.get("type") or "",
            "title": entry.get("title") or "",
            "evidence": evidence["text"],
            "evidence_clips": entry.get("evidence_clips") or [],
            "cue_ids": [str(cue_id) for cue_id in evidence["cue_ids"]],
        }
    return rendered


def item7_evidence(*, spoke_count, attended_count, engagement_percentage,
                   spoke_labels, silent_names) -> str:
    """
    The legacy deterministic Item 7 evidence string, verbatim in shape:

        Engagement score = {spoke} / {attended} = {percent}%.
        Students who spoke ({n}): {names}.
        Students who attended but did not speak ({n}): {names}.

    Built from the PERSISTED Phase 2C3 snapshot and Phase 2C4 result - never
    from live attendance. Legacy listed the VTT speaker labels for those who
    spoke and the attendance names for those who did not; that is reproduced.

    Legacy emitted the silent list in raw database order, which is not
    reproducible, so the platform orders it deterministically by name. The
    names appear only in this rendered output, never in logs or run metadata.
    """
    percent = engagement_percentage
    if percent is not None and str(percent).endswith(".00"):
        # Legacy printed a JS number: 100.00 rendered as "100", 35.71 as "35.71".
        percent = str(percent)[:-3]
    spoke_text = ", ".join(spoke_labels) or "(none)"
    silent_text = ", ".join(silent_names) or "(none)"
    return (f"Engagement score = {spoke_count} / {attended_count} = {percent}%.\n"
            f"Students who spoke ({len(spoke_labels)}): {spoke_text}.\n"
            f"Students who attended but did not speak ({len(silent_names)}): "
            f"{silent_text}.")


def render_fingerprint(*, renderer_version: str, evaluation_id, evaluation_fingerprint: str,
                       document_fingerprint: str, engagement_fingerprint,
                       lms_fingerprint, checklist, clip_states) -> str:
    """
    SHA-256 over every material renderer input, newline-joined in a fixed order:
    renderer version, the evaluation and its fingerprint, the canonical cue
    document fingerprint, the engagement fingerprint, the LMS snapshot
    fingerprint, the final checklist statuses, and each evidence clip with its
    validation state.

    A change to any of them creates new rendered provenance instead of
    overwriting the previous output.
    """
    lines = [
        f"renderer:{renderer_version}",
        f"evaluation:{evaluation_id}:{evaluation_fingerprint}",
        f"document:{document_fingerprint}",
        f"engagement:{engagement_fingerprint or ''}",
        f"lms:{lms_fingerprint or ''}",
        *[f"checklist:{order}:{status}" for order, status in sorted(checklist)],
        *sorted(f"clip:{clip_id}:{status}" for clip_id, status in clip_states),
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
