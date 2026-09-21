"""
The single place public.qa_perfect_lectures is mapped, typed and digested.

The contract comes from the executable legacy node `Upsert Perfect Lecture` in
QA_One_Lecture_Safe_Exact_Recording_v8, which maps eleven columns keyed on
`lecture_key`. Nothing else in the codebase may write that table.

Three facts about the live table shape this mapping:

- `recording_url` is owned by the RECORDING workflows, not by QA. The Master's
  `Update Both Recording Tables` fills it after a recording is matched, and
  the live data shows it diverging from the session's own recording_url on 45
  of 118 populated rows. The coded writer therefore never sets it, and the
  legacy upsert's COALESCE(EXCLUDED.recording_url, existing) means passing
  NULL preserves whatever the recording workflow put there.
- `recap_url`, `detected_at` and `excel_synced_at` are likewise foreign:
  `detected_at` defaults, and `excel_synced_at` belongs to the active
  "QA Perfect Lectures - Excel Sync" workflow. None is written or cleared.
- `session_date`, `subject` and `lecture_key` are the row's identity to legacy
  and are absent from the legacy DO UPDATE list, so an update never moves a
  row to a different lecture.
"""
import hashlib

from app.qa.perfect import PERFECT_ELIGIBILITY_VERSION, legacy_lecture_key
from app.writer.mapping import PayloadInvariantError, _canonical


PERFECT_WRITER_VERSION = "legacy_qa_v8_perfect_writer_v1"

# The legacy mapping CONTRACT version, separate from the writer version.
#
# v1 omitted attended_count: `perfect_row` read it with .get() while the
# rendered-payload loader never selected the column, so the first production
# row was written with NULL instead of 7. v2 is the corrected contract.
#
# It is deliberately NOT part of the writer version: a mapping correction must
# not create a second ownership identity for a row the platform already owns.
PERFECT_MAPPING_VERSION = "legacy_qa_v8_perfect_mapping_v2"

LEGACY_PERFECT_TABLE = "public.qa_perfect_lectures"
LEGACY_PERFECT_KEY = "lecture_key"

# The eleven columns `Upsert Perfect Lecture` maps, in export order.
PERFECT_COLUMNS = (
    "lecture_key", "session_date", "subject", "module", "trainer", "engagement",
    "attended_count", "met_count", "recording_url", "meeting_id", "session_id",
)

# Of those, the ones the CODED platform owns and may supply a value for.
CODED_OWNED_COLUMNS = (
    "lecture_key", "session_date", "subject", "module", "trainer", "engagement",
    "attended_count", "met_count", "meeting_id", "session_id",
)

# Owned by the recording workflows (Master `Update Both Recording Tables`,
# and legacy's own recording lookup). Never set, never cleared by this writer.
RECORDING_OWNED_COLUMNS = ("recording_url", "recap_url")

# Owned by the active Excel Sync workflow; and `detected_at` is a DB default.
FOREIGN_OWNED_COLUMNS = ("excel_synced_at", "detected_at", "id")

# Legacy's DO UPDATE list, split by how it merges. Columns in neither list -
# lecture_key, session_date, subject - are never updated at all.
PERFECT_UPDATE_REPLACE_COLUMNS = ("engagement", "attended_count", "met_count")
PERFECT_UPDATE_COALESCE_COLUMNS = ("recording_url", "meeting_id", "session_id",
                                   "module", "trainer")


def perfect_row(rendered: dict) -> dict:
    """
    Map one `lecture_qa_rendered_sessions` row onto the legacy Perfect Lecture
    columns.

    `lecture_key`, `session_date`, `subject` and `met_count` are NOT NULL in
    the live table, so a missing one is a refusal rather than something
    improvised into production.
    """
    if not rendered.get("session_id"):
        raise PayloadInvariantError("rendered payload has no legacy session_id")
    if not rendered.get("meeting_id"):
        # Legacy throws here too: "Cannot save recording metadata without
        # meeting_id and session_id."
        raise PayloadInvariantError("rendered payload has no legacy meeting_id")
    if rendered.get("legacy_date") in (None, ""):
        raise PayloadInvariantError("qa_perfect_lectures.session_date is NOT NULL")
    if not str(rendered.get("subject") or "").strip():
        raise PayloadInvariantError("qa_perfect_lectures.subject is NOT NULL")
    if rendered.get("met_count") is None:
        raise PayloadInvariantError("qa_perfect_lectures.met_count is NOT NULL")

    return {
        # Derived exactly as the legacy child derives it, from the same `date`
        # value that goes into qa_doctors_sessions.date.
        "lecture_key": legacy_lecture_key(rendered["legacy_date"], rendered["subject"]),
        "session_date": rendered["legacy_date"],
        "subject": rendered["subject"],
        # Legacy's `module` is the LMS module; the live table agrees with
        # qa_doctors_sessions.lms_module on all 35 rows where it is populated.
        "module": rendered.get("lms_module"),
        "trainer": rendered.get("trainer"),
        "engagement": rendered.get("engagement"),
        "attended_count": rendered.get("attended_count"),
        "met_count": rendered["met_count"],
        # Recording-owned. Always NULL from here, in every mode.
        "recording_url": None,
        "meeting_id": rendered["meeting_id"],
        "session_id": rendered["session_id"],
    }


def perfect_digest(row) -> str:
    """
    Deterministic digest of one legacy Perfect Lecture target.

    Covers only the eleven mapped columns, so a later recording or Excel-sync
    update to a column this writer does not own is visible where it belongs
    (recording_url is mapped and therefore included; recap_url, detected_at
    and excel_synced_at are not part of the QA contract at all).
    """
    if row is None:
        return hashlib.sha256(b"perfect:ABSENT").hexdigest()
    lines = [f"perfect:{column}={_canonical(row.get(column))}"
             for column in PERFECT_COLUMNS]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def diff_perfect(proposed: dict, existing: dict | None) -> dict:
    """
    Field-level comparison, restricted to the columns the coded writer owns.

    recording_url is excluded on purpose: a difference there is the recording
    workflow doing its job, not drift this writer should report or repair.
    """
    if existing is None:
        return {column: {"existing": None, "proposed": _canonical(proposed.get(column))}
                for column in CODED_OWNED_COLUMNS}
    return {
        column: {"existing": _canonical(existing.get(column)),
                 "proposed": _canonical(proposed.get(column))}
        for column in CODED_OWNED_COLUMNS
        if _canonical(existing.get(column)) != _canonical(proposed.get(column))
    }


def perfect_mapped_digest(row, *, mapping_version: str = PERFECT_MAPPING_VERSION) -> str:
    """
    Digest of the CODED-OWNED mapped payload only, plus the mapping version.

    This - not the source fingerprint, and not the whole-row digest - is what
    decides whether a coded-owned target still matches what this writer would
    write today.

    Two exclusions make it safe:

    * foreign columns are absent. recording_url, recap_url, excel_synced_at,
      detected_at and id belong to the recording and Excel Sync workflows, so
      a legitimate enrichment must never read as drift and must never provoke
      a rewrite. The whole-row `perfect_digest` still records them, as audit
      evidence of what the row looked like.
    * the mapping version participates, so a contract change is visible even
      in the pathological case where the mapped values happen to collide.
    """
    if row is None:
        return hashlib.sha256(b"perfect-mapped:ABSENT").hexdigest()
    lines = [f"mapping:{mapping_version}"]
    lines += [f"perfect:{column}={_canonical(row.get(column))}"
              for column in CODED_OWNED_COLUMNS]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def eligibility_identity() -> dict:
    """What produced an answer, for provenance on every audit row."""
    return {"eligibility_version": PERFECT_ELIGIBILITY_VERSION,
            "perfect_writer_version": PERFECT_WRITER_VERSION,
            "mapping_version": PERFECT_MAPPING_VERSION}
