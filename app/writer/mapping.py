"""
The single place the legacy QA columns are mapped, typed and digested.

Mappings come from the export: `Upsert QA Session` maps 22 columns keyed on
`session_id`, and `Upsert QA Checklist Items` maps 6 columns keyed on
`session_id_match`. Nothing else in the codebase may write those tables, so
the contract lives here and only here.

Two facts about the live legacy schema shape the mapping:

- `cancelled_session` is TEXT, and n8n wrote the JavaScript booleans
  'true' / 'false' into it, so a Python bool must be rendered the same way;
- the sessions table also carries clips_* and recording_* columns owned by
  OTHER workflows. An update therefore touches ONLY the 22 mapped columns and
  leaves everything else exactly as it was.
"""
import hashlib
import json
from decimal import Decimal, InvalidOperation

from app.qa.checklist import CHECKLIST_ITEM_COUNT


WRITER_VERSION = "legacy_qa_v8_writer_v1"

LEGACY_SESSION_TABLE = "public.qa_doctors_sessions"
LEGACY_CHECKLIST_TABLE = "public.qa_doctors_checklist_items"
LEGACY_SESSION_KEY = "session_id"
LEGACY_CHECKLIST_KEY = "session_id_match"

# The 22 columns `Upsert QA Session` mapped, in export order. Columns absent
# here - clips_*, recording_*, transcript_*, cancellation_reason - belong to
# other workflows and are never written or cleared by this writer.
SESSION_COLUMNS = (
    "session_id", "not_met_count", "partial_count", "met_count", "meeting_id",
    "trainer", "duration", "Engagement", "date", "subject", "lms_students_count",
    "lms_module", "lms_students", "duration_score", "engagement_score",
    "ksb_coverage", "strengths", "areas_for_development", "overall_judgement",
    "teaching_quality_rating", "teaching_quality_comments", "cancelled_session",
)
SESSION_JSON_COLUMNS = ("lms_students", "ksb_coverage", "strengths", "areas_for_development")

# The 6 columns `Upsert QA Checklist Items` mapped.
CHECKLIST_COLUMNS = ("session_id_match", "session_id", "checklist_item", "status",
                     "checklist_order", "evidence")


class PayloadInvariantError(RuntimeError):
    """The rendered payload cannot be written safely."""


def legacy_boolean(value) -> str | None:
    """`cancelled_session` is TEXT holding the JS booleans n8n wrote."""
    if value is None:
        return None
    return "true" if bool(value) else "false"


def session_row(rendered: dict) -> dict:
    """
    Map one `lecture_qa_rendered_sessions` row onto the legacy session columns.

    `trainer` is NOT NULL in the legacy table, so a missing trainer is a
    refusal rather than an empty string written into production.
    """
    if not rendered.get("session_id"):
        raise PayloadInvariantError("rendered payload has no legacy session_id")
    if not rendered.get("trainer"):
        raise PayloadInvariantError("legacy qa_doctors_sessions.trainer is NOT NULL")
    return {
        "session_id": rendered["session_id"],
        "not_met_count": rendered["not_met_count"],
        "partial_count": rendered["partial_count"],
        "met_count": rendered["met_count"],
        "meeting_id": rendered["meeting_id"],
        "trainer": rendered["trainer"],
        "duration": rendered["duration"],
        # Legacy column name is capitalised and must stay quoted in SQL.
        "Engagement": rendered["engagement"],
        # Legacy `date` semantics: the UTC date of the transcript start.
        "date": rendered["legacy_date"] or None,
        "subject": rendered["subject"],
        "lms_students_count": rendered["lms_students_count"],
        "lms_module": rendered["lms_module"],
        "lms_students": rendered["lms_students"],
        "duration_score": rendered["duration_score"],
        "engagement_score": rendered["engagement_score"],
        "ksb_coverage": rendered["ksb_coverage"],
        "strengths": rendered["strengths"],
        "areas_for_development": rendered["areas_for_development"],
        "overall_judgement": rendered["overall_judgement"],
        "teaching_quality_rating": rendered["teaching_quality_rating"],
        "teaching_quality_comments": rendered["teaching_quality_comments"],
        "cancelled_session": legacy_boolean(rendered["cancelled_session"]),
    }


def checklist_rows(rendered_items, session_id: str) -> list[dict]:
    """
    Map the rendered checklist onto the legacy checklist columns.

    Enforces the invariant before anything can be written: exactly eleven rows,
    orders 1..11 with no gaps, unique `session_id_match` keys that match the
    session, and a non-null status and item on each.
    """
    rows = [{
        "session_id_match": item["session_id_match"],
        "session_id": item["session_id"],
        "checklist_item": item["checklist_item"],
        "status": item["status"],
        "checklist_order": item["checklist_order"],
        # Written exactly as Phase 3B rendered it; never regenerated here.
        "evidence": item["evidence"],
    } for item in sorted(rendered_items, key=lambda item: item["checklist_order"])]
    validate_checklist(rows, session_id)
    return rows


def validate_checklist(rows, session_id: str) -> None:
    if len(rows) != CHECKLIST_ITEM_COUNT:
        raise PayloadInvariantError(
            f"expected {CHECKLIST_ITEM_COUNT} checklist rows, found {len(rows)}")
    orders = [row["checklist_order"] for row in rows]
    if orders != list(range(1, CHECKLIST_ITEM_COUNT + 1)):
        raise PayloadInvariantError(f"checklist orders must be 1..11, found {orders}")
    keys = {row["session_id_match"] for row in rows}
    if len(keys) != CHECKLIST_ITEM_COUNT:
        raise PayloadInvariantError("session_id_match values are not unique")
    expected = {f"{session_id}_{order}" for order in orders}
    if keys != expected:
        raise PayloadInvariantError("session_id_match does not match session_id_order")
    for row in rows:
        if not row["status"] or not row["checklist_item"] or not row["session_id"]:
            raise PayloadInvariantError(
                f"checklist row {row['checklist_order']} has a NOT NULL violation")


def payload_is_valid(rendered: dict, rendered_items) -> tuple[bool, str | None]:
    """Non-raising invariant check, for planning a dry run."""
    try:
        session_row(rendered)
        checklist_rows(rendered_items, rendered["session_id"])
    except PayloadInvariantError as error:
        return False, str(error)
    return True, None


def _canonical(value):
    """
    Stable serialization: JSON by sorted keys, numerics by value, else text.

    Numerics are normalized because the legacy column is unconstrained
    `numeric` while the rendered column is `numeric(5,2)`: Decimal("100") and
    Decimal("100.00") are the same Engagement and must not be reported as a
    difference.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return legacy_boolean(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    if isinstance(value, (Decimal, float, int)):
        return _canonical_number(value)
    text = str(value)
    try:
        # Legacy text columns can still hold a number-shaped value.
        return _canonical_number(Decimal(text))
    except (InvalidOperation, ValueError):
        return text


def _canonical_number(value) -> str:
    number = Decimal(str(value)).normalize()
    # normalize() renders 100 as 1E+2; expand it back to a plain form.
    return format(number, "f")


def digest(session, checklist) -> str:
    """
    Deterministic digest of one legacy target: the mapped session columns plus
    the eleven checklist rows in order.

    Used as the pre-write and post-write digest, so a write can be shown to
    have changed exactly what was intended - and a dry run can prove it changed
    nothing.
    """
    lines = []
    if session is None:
        lines.append("session:ABSENT")
    else:
        lines += [f"session:{column}={_canonical(session.get(column))}"
                  for column in SESSION_COLUMNS]
    for row in sorted(checklist or [], key=lambda item: item["checklist_order"]):
        lines += [f"item:{row['checklist_order']}:{column}={_canonical(row.get(column))}"
                  for column in CHECKLIST_COLUMNS]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def diff_session(proposed: dict, existing: dict | None) -> dict:
    """Field-level comparison over the mapped columns only."""
    if existing is None:
        return {column: {"existing": None, "proposed": _canonical(proposed.get(column))}
                for column in SESSION_COLUMNS}
    return {
        column: {"existing": _canonical(existing.get(column)),
                 "proposed": _canonical(proposed.get(column))}
        for column in SESSION_COLUMNS
        if _canonical(existing.get(column)) != _canonical(proposed.get(column))
    }


def diff_checklist(proposed_rows, existing_rows) -> dict:
    """Per-order status and evidence differences."""
    existing = {row["checklist_order"]: row for row in (existing_rows or [])}
    status_differences = []
    evidence_differences = []
    missing = []
    for row in proposed_rows:
        order = row["checklist_order"]
        current = existing.get(order)
        if current is None:
            missing.append(order)
            continue
        if _canonical(current.get("status")) != _canonical(row.get("status")):
            status_differences.append(order)
        if _canonical(current.get("evidence")) != _canonical(row.get("evidence")):
            evidence_differences.append(order)
    return {"status_difference_orders": status_differences,
            "evidence_difference_orders": evidence_differences,
            "missing_orders": missing,
            "status_difference_count": len(status_differences),
            "evidence_difference_count": len(evidence_differences)}
