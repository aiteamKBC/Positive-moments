"""
Recording Links coverage: what the Operations console shows about recordings.

ONE PLACE DECIDES WHAT A ROW MEANS
----------------------------------
The Historical Backfill page reports recording coverage for a date range, per
lecture and in total. Every word and every number it shows is decided here, in
Python, from backend truth - never in the browser:

  * a PREVIEW run evaluates each lecture LIVE through the same
    `RecordingLinkPreview` the `recording-links-preview` CLI uses (Graph reads
    only, no publisher, a read-only connection);
  * an EXECUTE run reads what the shared orchestrator actually did, from the
    stage's own durable state (`lecture_recording_links`) and the resolver.

`outcome_for` folds the stage's exact statuses (app/recordings/models.py) into
one operator-facing outcome per row, and `summarize` counts those outcomes.
The outcomes are a PARTITION - every row lands in exactly one - so the totals
always reconcile with the table beneath them. The exact domain status travels
alongside, so nothing is hidden behind the summary word.

Nothing here carries a recording URL, an item or drive id, a file name, a
token or transcript text.
"""
from __future__ import annotations

from collections import Counter

from app.orchestration.stages import (
    COMPLETE,
    IDLE_ACTIONS,
    NOT_APPLICABLE,
    RECORDING_LINK,
    REVIEW_REQUIRED,
    STAGE_ORDER,
    WAITING,
)
from app.recordings import models as m
from app.recordings.preview import (
    CODED_LECTURE,
    LECTURES_IN_RANGE,
    LEGACY_ROW_ONLY,
    LEGACY_ROWS_IN_RANGE,
    LIVE_VERIFIED,
    legacy_only_rows,
)

# -- the operator-facing outcome of one row --------------------------------------
ALREADY_LINKED = "ALREADY_LINKED"
WRITTEN = "WRITTEN"
EXACT_MATCH = "EXACT_MATCH"
AMBIGUOUS = "AMBIGUOUS"
BLOCKED_BY_EARLIER_STAGE = "BLOCKED_BY_EARLIER_STAGE"
REVIEW = "REVIEW_REQUIRED"
NOT_APPLICABLE_OUTCOME = "NOT_APPLICABLE"
NOT_FOUND = "NOT_FOUND"
GRAPH_LOOKUP_FAILED = "GRAPH_LOOKUP_FAILED"
DISCOVERY_FAILED = "DISCOVERY_FAILED"
NO_CODED_LECTURE = "NO_CODED_LECTURE"
NO_LEGACY_TARGET = "NO_LEGACY_TARGET"
WAITING_OUTCOME = "WAITING"
# An execute run in `observe` mode: the n8n branch still owns the write, so the
# coded stage never evaluated this lecture.
NOT_EVALUATED = "NOT_EVALUATED"
OTHER = "OTHER"

OUTCOMES = (ALREADY_LINKED, WRITTEN, EXACT_MATCH, AMBIGUOUS, BLOCKED_BY_EARLIER_STAGE,
            REVIEW, NOT_APPLICABLE_OUTCOME, NOT_FOUND, GRAPH_LOOKUP_FAILED,
            DISCOVERY_FAILED, NO_CODED_LECTURE, NO_LEGACY_TARGET, WAITING_OUTCOME,
            NOT_EVALUATED, OTHER)

EVALUATION_LIVE_PREVIEW = "LIVE_PREVIEW"
EVALUATION_EXECUTE = "EXECUTE"

_ALREADY = {m.RECORDING_ALREADY_LINKED, m.WRITE_REFUSED_ALREADY_LINKED}
_AMBIGUOUS = {m.GRAPH_RECORDING_AMBIGUOUS, m.AMBIGUOUS_RECORDING_FILES}
_NOT_FOUND = {m.GRAPH_RECORDING_NOT_FOUND, m.RECORDING_FILE_NOT_FOUND, m.SUBJECT_MISMATCH}
_REVIEW = {m.TIMESTAMP_MISMATCH, m.ORGANIZER_LOOKUP_ID_MISSING,
           m.SESSION_CALL_ID_MISSING, m.LECTURE_IDENTITY_MISMATCH}


def outcome_for(item: dict) -> str:
    """
    The one outcome for a row, in precedence order.

    A written or already-present link wins over everything; a cancelled lecture
    is not a gap; an earlier unfinished stage is reported as the reason before
    any Graph verdict, because the stage cannot act until that settles; then the
    stage's own verdict, most decisive first.
    """
    status = item.get("recording_status")
    stage = item.get("recording_stage_state")
    if item.get("population") == LEGACY_ROW_ONLY:
        if status == m.RECORDING_ALREADY_LINKED:
            return ALREADY_LINKED
        if item.get("legacy_cancelled"):
            return NOT_APPLICABLE_OUTCOME
        return NO_CODED_LECTURE
    if item.get("written"):
        return WRITTEN
    if stage == COMPLETE or status in _ALREADY:
        return ALREADY_LINKED
    if status == m.NO_RECORDING_EXPECTED_CANCELLED or (
            stage == NOT_APPLICABLE
            and item.get("stage_reason") == m.NO_RECORDING_EXPECTED_CANCELLED):
        return NOT_APPLICABLE_OUTCOME
    # Ambiguity outranks a pending earlier stage: finishing that stage will not
    # make two candidates one, and the operator needs to know it now.
    if status in _AMBIGUOUS:
        return AMBIGUOUS
    if item.get("earlier_stage"):
        return BLOCKED_BY_EARLIER_STAGE
    if status == m.NO_LEGACY_QA_ROW or stage == NOT_APPLICABLE:
        return NO_LEGACY_TARGET
    if status == m.EXACT_RECORDING_FILE_MATCHED:
        return EXACT_MATCH if item.get("would_write") else OTHER
    if stage == REVIEW_REQUIRED or status in _REVIEW:
        return REVIEW
    if status == m.GRAPH_LOOKUP_FAILED:
        return GRAPH_LOOKUP_FAILED
    if status == m.DRIVE_ITEM_DISCOVERY_FAILED:
        return DISCOVERY_FAILED
    if status in _NOT_FOUND:
        return NOT_FOUND
    if status == m.LINK_URL_UNAVAILABLE or stage == WAITING:
        return WAITING_OUTCOME
    if status is None:
        return NOT_EVALUATED
    return OTHER


def earlier_stage_of(state: dict) -> tuple[str | None, str | None]:
    """
    The unfinished stage the pipeline must act on BEFORE RECORDING_LINK.

    Read straight from the resolver's own verdict. A wait is not a blocker -
    the resolver steps over waits - so only a real action on an earlier stage
    (including MANUAL_REVIEW_REQUIRED) counts.
    """
    stage = state.get("executable_stage")
    action = state.get("next_executable_action")
    if not stage or stage == RECORDING_LINK or action in IDLE_ACTIONS:
        return None, None
    if stage in STAGE_ORDER and STAGE_ORDER.index(stage) < STAGE_ORDER.index(RECORDING_LINK):
        return stage, action
    return None, None


def _needs_link(stage_state, reason) -> bool:
    """Still expecting a link: not present and not cancelled. A missing legacy
    row counts, because it is usually an earlier stage that has not run yet."""
    return stage_state != COMPLETE and reason != m.NO_RECORDING_EXPECTED_CANCELLED


def _key(lecture_id, legacy_ref) -> str:
    return str(lecture_id) if lecture_id else f"legacy:{legacy_ref}"


# -- rows ----------------------------------------------------------------------------

def item_from_preview_row(row: dict, *, mode: str) -> dict:
    """A live preview row, reduced to what the console may show."""
    coded = row.get("population") == CODED_LECTURE
    stage_state = row.get("resolver_stage_state") if coded else None
    earlier_stage = earlier_action = None
    if coded and _needs_link(stage_state, row.get("resolver_stage_reason")):
        earlier_stage, earlier_action = earlier_stage_of(row)
    item = {
        "item_key": _key(row.get("lecture_id"), row.get("legacy_session_ref")),
        "lecture_id": row.get("lecture_id"),
        "business_date": row.get("date"),
        "subject": row.get("subject"),
        "population": row.get("population"),
        "recording_link_mode": mode,
        "evaluation": EVALUATION_LIVE_PREVIEW,
        "recording_stage_state": stage_state,
        "stage_reason": row.get("resolver_stage_reason") if coded else None,
        "recording_status": row.get("recording_match_status"),
        "reason": row.get("reason"),
        "earlier_stage": earlier_stage,
        "earlier_action": earlier_action,
        "would_write": bool(row.get("would_write")),
        "written": False,
        "perfect_row_updated": False,
        "perfect_row_would_update": bool(row.get("would_update_perfect")),
        "source": row.get("recording_source"),
        "timestamp_difference_seconds": row.get("timestamp_difference_seconds"),
        "candidate_file_count": row.get("candidate_file_count"),
        "exact_candidate_count": row.get("exact_candidate_count"),
        "graph_lookup_status": row.get("graph_lookup_status"),
        "graph_http_status": row.get("graph_http_status"),
        "verification": row.get("verification"),
        "attempt_count": None,
        "next_attempt_after": None,
        "last_attempted_at": None,
        "legacy_cancelled": bool(row.get("legacy_cancelled")),
    }
    item["outcome"] = outcome_for(item)
    return item


def execute_items(connection, day, *, resolver, repository, mode: str,
                  written_since) -> list[dict]:
    """
    What an EXECUTE day actually left behind, read after the orchestrator ran.

    The resolver answers the stage state and whether an earlier stage still
    blocks; the stage's durable row answers the last verdict and whether THIS
    run wrote the link (`written_at` at or after the day started).
    """
    legacy = connection.execute(LEGACY_ROWS_IN_RANGE, (day, day)).fetchall()
    lecture_ids = [str(row[0]) for row in
                   connection.execute(LECTURES_IN_RANGE, (day, day)).fetchall()]
    states, owned = [], set()
    for lecture_id in lecture_ids:
        state = resolver.for_lecture(connection, lecture_id)
        if state.get("is_suppressed_duplicate"):
            continue
        states.append(state)
        session_id = state["stages"][RECORDING_LINK].get("legacy_session_id")
        if session_id:
            owned.add(session_id)
    details = (repository.details(connection, [s["lecture_id"] for s in states])
               if mode == "write" else {})

    items = []
    for state in states:
        stage = state["stages"][RECORDING_LINK]
        session_id = stage.get("legacy_session_id")
        detail = details.get(str(state["lecture_id"]))
        if detail and detail.get("legacy_session_id") != session_id:
            detail = None                  # evaluated against a different legacy row
        written_at = (detail or {}).get("written_at")
        written = bool(detail and detail.get("recording_url_written") and written_at
                       and written_since is not None and written_at >= written_since)
        earlier_stage = earlier_action = None
        if _needs_link(stage["state"], stage.get("reason")):
            earlier_stage, earlier_action = earlier_stage_of(state)
        item = {
            "item_key": _key(state["lecture_id"], None),
            "lecture_id": str(state["lecture_id"]),
            "business_date": state["session_date"],
            "subject": state["subject"],
            "population": CODED_LECTURE,
            "recording_link_mode": mode,
            "evaluation": EVALUATION_EXECUTE,
            "recording_stage_state": stage["state"],
            "stage_reason": stage.get("reason"),
            "recording_status": (detail or {}).get("status") or _status_from_stage(stage),
            "reason": (detail or {}).get("reason") or stage.get("reason"),
            "earlier_stage": earlier_stage,
            "earlier_action": earlier_action,
            "would_write": False,
            "written": written,
            "perfect_row_updated": bool(written and (detail or {}).get("perfect_rows_written")),
            "perfect_row_would_update": False,
            "source": (detail or {}).get("source"),
            "timestamp_difference_seconds": (detail or {}).get("timestamp_difference_seconds"),
            "candidate_file_count": (detail or {}).get("candidate_file_count"),
            "exact_candidate_count": (detail or {}).get("exact_candidate_count"),
            "graph_lookup_status": (detail or {}).get("graph_lookup_status"),
            "graph_http_status": (detail or {}).get("graph_http_status"),
            "verification": LIVE_VERIFIED if detail else None,
            "attempt_count": (detail or {}).get("attempt_count"),
            "next_attempt_after": (detail or {}).get("next_attempt_after"),
            "last_attempted_at": (detail or {}).get("last_attempted_at"),
            "legacy_cancelled": False,
        }
        item["outcome"] = outcome_for(item)
        items.append(item)
    for row in legacy_only_rows(legacy, owned):
        item = item_from_preview_row(row, mode=mode)
        item["evaluation"] = EVALUATION_EXECUTE
        items.append(item)
    return items


def _status_from_stage(stage: dict) -> str | None:
    """A status the resolver already knows without a stored evaluation."""
    if stage["state"] == COMPLETE:
        return m.RECORDING_ALREADY_LINKED
    if stage.get("reason") in (m.NO_RECORDING_EXPECTED_CANCELLED, m.NO_LEGACY_QA_ROW):
        return stage["reason"]
    return None


# -- totals ---------------------------------------------------------------------------

def _percent(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def summarize(items: list[dict]) -> dict:
    """
    Coverage for a set of rows. Every count is a count of `outcome_for` - the
    partition - except `exact_matched`, which also includes exact matches still
    waiting behind an earlier stage.
    """
    by = Counter(item["outcome"] for item in items)
    total = len(items)
    not_applicable = by[NOT_APPLICABLE_OUTCOME]
    eligible = total - not_applicable
    already = by[ALREADY_LINKED]
    written = by[WRITTEN]
    would_write = by[EXACT_MATCH]
    missing_before = eligible - already
    return {
        "total": total,
        "not_applicable": not_applicable,
        "eligible": eligible,
        "already_linked": already,
        "missing_before": missing_before,
        "exact_matched": sum(1 for item in items if item.get("written") or
                             item.get("recording_status") in (
                                 m.EXACT_RECORDING_FILE_MATCHED, m.WRITTEN)),
        "would_write": would_write,
        "written": written,
        "perfect_rows_updated": sum(1 for item in items if item.get("perfect_row_updated")),
        "perfect_rows_would_update": sum(
            1 for item in items
            if item["outcome"] == EXACT_MATCH and item.get("perfect_row_would_update")),
        "ambiguous": by[AMBIGUOUS],
        "blocked_by_earlier_stage": by[BLOCKED_BY_EARLIER_STAGE],
        "review_required": by[REVIEW],
        "not_found": by[NOT_FOUND],
        "graph_lookup_failed": by[GRAPH_LOOKUP_FAILED],
        "discovery_failed": by[DISCOVERY_FAILED],
        "no_coded_lecture": by[NO_CODED_LECTURE],
        "no_legacy_target": by[NO_LEGACY_TARGET],
        "waiting": by[WAITING_OUTCOME],
        "not_evaluated": by[NOT_EVALUATED],
        "other": by[OTHER],
        "still_missing_after": missing_before - written,
        "projected_missing_after_write": missing_before - written - would_write,
        "coverage_percent_before": _percent(already, eligible),
        "coverage_percent_after": _percent(already + written, eligible),
        "projected_coverage_percent": _percent(already + written + would_write, eligible),
        "by_outcome": {outcome: by[outcome] for outcome in OUTCOMES if by[outcome]},
        # The partition check, stated rather than assumed.
        "reconciles": sum(by.values()) == total,
        "recording_link_modes": sorted({item.get("recording_link_mode")
                                        for item in items if item.get("recording_link_mode")}),
    }


# -- the Historical Backfill hook ------------------------------------------------------

class BackfillRecordingReport:
    """
    What the backfill runner asks about recordings, once per day.

    `preview_factory` builds a fresh live `RecordingLinkPreview` per day (so a
    long-lived runner never serves a stale folder listing); `resolver_factory`
    builds the resolver an EXECUTE snapshot reads through, in the configured
    RECORDING_LINK_MODE.
    """

    def __init__(self, *, mode: str, preview_factory, resolver_factory, repository=None):
        self.mode = mode
        self.preview_factory = preview_factory
        self.resolver_factory = resolver_factory
        if repository is None:
            from app.db.repositories.recording_links import RecordingLinkRepository
            repository = RecordingLinkRepository()
        self.repository = repository

    def preview_day(self, connection, day) -> tuple[list[dict], int]:
        preview = self.preview_factory()
        rows, _ = preview.rows(connection, day, day)
        return ([item_from_preview_row(row, mode=self.mode) for row in rows],
                preview.service.graph_calls)

    def execute_day(self, connection, day, *, written_since) -> list[dict]:
        return execute_items(connection, day, resolver=self.resolver_factory(),
                             repository=self.repository, mode=self.mode,
                             written_since=written_since)
