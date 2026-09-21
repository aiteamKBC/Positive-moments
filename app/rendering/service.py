"""
Phase 3B orchestration: validated Phase 3A evidence -> legacy-compatible output.

Per lecture:

  1. take the Phase 3A evaluation; only COMPLETED and NON_DELIVERED are
     renderable, and a delivered lecture without one is reported as
     SOURCE_QA_NOT_READY - never evaluated here;
  2. capture or reuse the frozen LMS snapshot (the live table is read once,
     then never again while a snapshot exists);
  3. render evidence text from validated clips plus canonical cues;
  4. build the legacy session and checklist compatibility payloads;
  5. persist to the shadow rendering tables only.

Never: a model call, Graph, WebVTT reparsing, live attendance, transcript
reselection, speaker rematching, engagement recalculation, or a write to a
Phase 3A or legacy QA table.
"""
import logging
import time
import uuid
from decimal import Decimal

from app.qa.checklist import CHECKLIST_ITEMS, ENGAGEMENT_ITEM
from app.qa.inputs import QA_ENGINE_VERSION, REQUIRED_ATTENDANCE_ROSTER_VERSION
from app.rendering.compatibility import (
    SEVERITY,
    counts_from_statuses,
    item7_evidence,
    ksbs_to_object,
    legacy_date,
    render_fingerprint,
    session_id_match,
    titled_items_to_object,
)
from app.rendering.evidence import (
    NO_EVIDENCE,
    RENDERER_VERSION,
    format_clips,
    legacy_evidence_field,
)
from app.rendering.lms import (
    LEGACY_ROW_CAP,
    LMS_SNAPSHOT_VERSION,
    compare_with_legacy,
    legacy_students_payload,
    lms_fingerprint,
    normalize_module,
)
from app.lectures.matching import normalize_group
from app.qa.evidence_policy import (
    DEFAULT_EVIDENCE_POLICY,
    EVIDENCE_POLICY_VERSIONS,
    assess as assess_evidence,
)


RENDERED = "RENDERED"
RENDERED_NON_DELIVERED = "RENDERED_NON_DELIVERED"
SOURCE_QA_NOT_READY = "SOURCE_QA_NOT_READY"
INCONSISTENT_EVIDENCE = "INCONSISTENT_EVIDENCE"

VALID_CLIP_STATUS = "VALID"
RENDERABLE_QA_STATUSES = ("COMPLETED", "NON_DELIVERED")

LMS_NAMESPACE = uuid.UUID("3f6b1e84-9d27-4c05-a71e-52d8c4a9b013")
RENDER_NAMESPACE = uuid.UUID("8c04d2f7-51ba-4e69-9f3d-6a72e0b18c45")


class RenderInputError(RuntimeError):
    """The persisted evidence cannot support rendering."""


class QaRenderingService:
    def __init__(self, *, input_repository, lms_source_repository, lms_snapshot_repository,
                 output_repository, run_repository, legacy_repository=None,
                 renderer_version: str = RENDERER_VERSION,
                 lms_snapshot_version: str = LMS_SNAPSHOT_VERSION,
                 qa_engine_version: str = QA_ENGINE_VERSION,
                 attendance_roster_version: str = REQUIRED_ATTENDANCE_ROSTER_VERSION,
                 evidence_policy_version: str = DEFAULT_EVIDENCE_POLICY,
                 refresh_lms: bool = False, lecture_ids=None):
        self.input_repository = input_repository
        self.lms_source_repository = lms_source_repository
        self.lms_snapshot_repository = lms_snapshot_repository
        self.output_repository = output_repository
        self.run_repository = run_repository
        self.legacy_repository = legacy_repository
        self.renderer_version = renderer_version
        self.lms_snapshot_version = lms_snapshot_version
        self.qa_engine_version = qa_engine_version
        self.attendance_roster_version = attendance_roster_version
        # Phase 4B: the renderer must judge evidence by the SAME rule Phase 3A
        # used. If the two disagreed, an evaluation could be COMPLETED and then
        # refuse to render forever - a lecture stuck between two policies.
        if evidence_policy_version not in EVIDENCE_POLICY_VERSIONS:
            raise RenderInputError(
                f"unknown evidence policy {evidence_policy_version}")
        self.evidence_policy_version = evidence_policy_version
        # Only an explicit refresh may re-read the live LMS table once a
        # snapshot exists for the lecture.
        self.refresh_lms = refresh_lms
        # Optional scope, matching the QA engine and the legacy writer, so a
        # controlled single-lecture run renders exactly that lecture.
        self.lecture_ids = {str(value) for value in lecture_ids} if lecture_ids else None
        self.log = logging.getLogger(__name__)

    def render_day(self, connection, target_date) -> dict:
        started = time.monotonic()
        run_id = self.run_repository.start(
            connection, target_date, renderer_version=self.renderer_version,
            lms_snapshot_version=self.lms_snapshot_version)

        evaluations = self.input_repository.load_inputs(
            connection, target_date,
            attendance_roster_version=self.attendance_roster_version,
            qa_engine_version=self.qa_engine_version)
        if not evaluations:
            raise RenderInputError(f"no Phase 3A evaluations for {target_date}")
        if self.lecture_ids is not None:
            evaluations = [row for row in evaluations
                           if str(row["lecture_id"]) in self.lecture_ids]
            if not evaluations:
                raise RenderInputError(
                    f"no Phase 3A evaluation for the requested lecture scope on {target_date}")

        counters = {name: 0 for name in (
            "lectures_considered", "sessions_rendered", "sessions_reused",
            "checklist_rows_rendered", "evidence_clips_consumed", "blocks_rendered",
            "lms_snapshots_created", "lms_snapshots_reused", "not_ready_count",
            "provider_calls", "error_count")}
        counters["lectures_considered"] = len(evaluations)
        results = []
        for evaluation in evaluations:
            results.append(self._render_one(connection, evaluation, counters))

        comparison = None
        if self.legacy_repository is not None:
            comparison = self._legacy_comparison(connection, target_date, results)

        summary = {
            "run_id": str(run_id), "target_date": target_date.isoformat(),
            "status": "COMPLETED", "renderer_version": self.renderer_version,
            "lms_snapshot_version": self.lms_snapshot_version,
            "qa_engine_version": self.qa_engine_version,
            "attendance_roster_version": self.attendance_roster_version,
            **counters,
            # Explicit zero markers for this phase's boundaries.
            "graph_calls": 0, "webvtt_reparsed": False, "live_attendance_queries": 0,
            "aptem_queries": 0, "transcript_reselections": 0, "speaker_rematches": 0,
            "engagement_recalculations": 0, "legacy_qa_writes": 0,
            "phase_3a_mutations": 0,
            "lectures": results, "legacy_comparison": comparison,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "metadata": {"renderer_version": self.renderer_version,
                         "lms_snapshot_version": self.lms_snapshot_version,
                         "counters": counters, "provider_calls": 0,
                         "legacy_qa_writes": 0},
        }
        self.run_repository.complete(connection, run_id, summary)
        # Counts and ids only: no learner names, no quotes, no transcript text.
        self.log.info("qa rendering completed", extra={"fields": {
            "service": "qa_rendering", "operation": "render_day", "run_id": str(run_id),
            "lectures": len(results), "blocks": counters["blocks_rendered"],
            "provider_calls": 0, "duration_ms": summary["duration_ms"],
        }})
        return summary

    def _render_one(self, connection, evaluation, counters) -> dict:
        subject = evaluation["subject"]
        result = {"subject": subject, "lecture_id": str(evaluation["lecture_id"]),
                  "evaluation_id": str(evaluation["evaluation_id"]),
                  "evaluation_status": evaluation["qa_status"],
                  "delivery_status": evaluation["delivery_status"],
                  "session_id": evaluation["primary_provider_transcript_id"]}

        if evaluation["qa_status"] not in RENDERABLE_QA_STATUSES:
            # A renderer never evaluates: an unusable evaluation is reported.
            counters["not_ready_count"] += 1
            result.update({"render_status": SOURCE_QA_NOT_READY, "checklist_rows": 0,
                           "evidence_clips": 0, "blocks_rendered": 0})
            return result

        non_delivered = evaluation["delivery_status"] == "NON_DELIVERED"
        stored = self.input_repository.load_clips(connection, evaluation["evaluation_id"])
        evidence = assess_evidence([clip["validation_status"] for clip in stored],
                                   policy=self.evidence_policy_version)
        if not evidence["evaluation_usable"]:
            # A FABRICATED coordinate means the answer cited somewhere that is
            # not in the transcript. Rendering that would publish evidence the
            # session never contained, so the render stops instead.
            counters["error_count"] += 1
            result.update({"render_status": INCONSISTENT_EVIDENCE,
                           "invalid_clip_count": evidence["fatal_clip_count"],
                           "evidence_assessment": evidence, "checklist_rows": 0,
                           "evidence_clips": len(stored), "blocks_rendered": 0})
            return result
        # Only VALID clips ever render. An excluded degenerate clip is dropped
        # here as well as in the verdict, so tolerating it in the evaluation can
        # never mean it sneaks into the published evidence.
        clips = [clip for clip in stored
                 if clip["validation_status"] == VALID_CLIP_STATUS]
        result["excluded_clip_count"] = evidence["excluded_clip_count"]
        result["evidence_assessment"] = evidence

        cues = ([] if non_delivered
                else self.input_repository.load_cues(connection, evaluation["document_id"]))
        checklist = self.input_repository.load_checklist(
            connection, evaluation["evaluation_id"])
        by_key = {}
        for clip in clips:
            by_key.setdefault((clip["clip_source"], clip["source_position"]), []).append(clip)

        blocks_total = 0

        def render(section, position):
            nonlocal blocks_total
            rendered = format_clips(
                sorted(by_key.get((section, position), []),
                       key=lambda clip: clip["clip_index"]), cues)
            blocks_total += rendered["block_count"]
            return rendered

        lms = (None if non_delivered
               else self._lms_snapshot(connection, evaluation, counters))

        rendered_checklist, engagement_evidence = self._render_checklist(
            connection, evaluation, checklist, render, non_delivered)
        counters["checklist_rows_rendered"] += len(rendered_checklist)

        output = evaluation["ai_raw_output"] or {}
        summary_block = output.get("overall_summary") or {}
        # The AI payload supplies titles and ordering; the stored clip rows
        # supply the validated timestamps, keyed by the same 1-based position.
        strengths = titled_items_to_object(
            summary_block.get("strengths"), "strength",
            lambda position: render("strengths", position))
        areas = titled_items_to_object(
            summary_block.get("areas_for_improvement"), "area",
            lambda position: render("areas_for_improvement", position))
        ksbs = ksbs_to_object(output.get("ksbs_covered"),
                              lambda position: render("ksb", position))
        teaching = render("teaching_quality", 1) if not non_delivered else {
            "text": NO_EVIDENCE, "block_count": 0, "cue_ids": [], "clip_ids": []}

        counts = counts_from_statuses(row["status"] for row in rendered_checklist)
        fingerprint = render_fingerprint(
            renderer_version=self.renderer_version,
            evaluation_id=evaluation["evaluation_id"],
            evaluation_fingerprint=evaluation["evaluation_fingerprint"],
            document_fingerprint=evaluation["document_fingerprint"] or "",
            engagement_fingerprint=evaluation["engagement_fingerprint"],
            lms_fingerprint=(lms or {}).get("source_fingerprint"),
            checklist=[(row["checklist_order"], row["status"]) for row in rendered_checklist],
            clip_states=[(clip["clip_id"], clip["validation_status"]) for clip in clips])

        session = {
            "rendered_session_id": uuid.uuid5(RENDER_NAMESPACE, fingerprint),
            "lecture_id": evaluation["lecture_id"],
            "evaluation_id": evaluation["evaluation_id"],
            "lms_snapshot_id": (lms or {}).get("snapshot_id"),
            "renderer_version": self.renderer_version,
            "render_status": RENDERED_NON_DELIVERED if non_delivered else RENDERED,
            "session_id": evaluation["primary_provider_transcript_id"],
            "meeting_id": evaluation["meeting_id"], "subject": subject,
            "trainer": evaluation["canonical_trainer"],
            "legacy_date": legacy_date(evaluation["actual_start"]),
            "canonical_session_date": evaluation["session_date"],
            "duration": evaluation["duration_text"],
            "duration_score": evaluation["duration_score"],
            "engagement": evaluation["engagement_percentage"],
            "engagement_score": evaluation["engagement_score"],
            "attended_count": evaluation["attended_count"],
            "spoke_count": evaluation["spoke_count"],
            **counts,
            "teaching_quality_rating": evaluation["teaching_quality_rating"],
            "teaching_quality_comments": evaluation["teaching_quality_comments"],
            "teaching_quality_evidence": teaching["text"],
            "overall_judgement": evaluation["overall_judgement"],
            "cancelled_session": bool(evaluation["cancelled_session"]),
            "lms_module": (lms or {}).get("module"),
            "lms_students_count": (lms or {}).get("student_count"),
            "lms_students": (lms or {}).get("students_payload"),
            "strengths": strengths, "areas_for_development": areas, "ksb_coverage": ksbs,
            "evidence_clip_count": len(clips), "rendered_block_count": blocks_total,
            "source_fingerprint": fingerprint,
            "metadata": {"renderer_version": self.renderer_version,
                         "lms_snapshot_version": self.lms_snapshot_version,
                         "item7_override_applied": bool(evaluation["item7_override_applied"]),
                         "item7_evidence_rendered": bool(engagement_evidence),
                         "canonical_session_date": str(evaluation["session_date"]),
                         "legacy_date_matches_session_date":
                             legacy_date(evaluation["actual_start"])
                             == str(evaluation["session_date"])},
        }
        written = self.output_repository.upsert(connection, session, rendered_checklist)
        counters["sessions_rendered"] += written["created"]
        counters["sessions_reused"] += written["reused"]
        counters["evidence_clips_consumed"] += len(clips)
        counters["blocks_rendered"] += blocks_total

        result.update({
            "render_status": session["render_status"],
            "rendered_session_id": str(written["rendered_session_id"]),
            "source_fingerprint": fingerprint,
            "source_fingerprint_prefix": fingerprint[:16],
            "checklist_rows": len(rendered_checklist),
            "evidence_clips": len(clips), "blocks_rendered": blocks_total,
            "evidence_strings": sum(1 for row in rendered_checklist
                                    if row["rendered_evidence"] != NO_EVIDENCE),
            "lms_student_count": (lms or {}).get("student_count"),
            "lms_snapshot_id": str((lms or {}).get("snapshot_id") or "") or None,
            "lms_snapshot_reused": bool(lms and lms.get("reused")),
            "legacy_date": session["legacy_date"],
            "canonical_session_date": str(evaluation["session_date"]),
            "met_partial_not_met": [counts["met_count"], counts["partial_count"],
                                    counts["not_met_count"]],
            "item2_status": next((row["status"] for row in rendered_checklist
                                  if row["checklist_order"] == 2), None),
            "item7_status": next((row["status"] for row in rendered_checklist
                                  if row["checklist_order"] == ENGAGEMENT_ITEM), None),
        })
        return result

    def _render_checklist(self, connection, evaluation, checklist, render, non_delivered):
        """Render the eleven rows, with the deterministic Item 7 evidence."""
        engagement_evidence = None
        if not non_delivered and evaluation["item7_override_applied"]:
            engagement_evidence = self._item7_evidence(connection, evaluation)

        rows = []
        for row in checklist:
            order = row["checklist_order"]
            if non_delivered:
                rendered = {"text": row["reasoning"] or NO_EVIDENCE, "block_count": 0,
                            "cue_ids": [], "clip_ids": []}
                # The legacy cancelled node wrote a fixed evidence sentence.
                rendered["text"] = "Session was cancelled or ended before delivery began."
            elif order == ENGAGEMENT_ITEM and engagement_evidence is not None:
                rendered = {"text": engagement_evidence, "block_count": 0,
                            "cue_ids": [], "clip_ids": []}
            else:
                rendered = render("checklist", order)
            evidence_field = (rendered["text"] if non_delivered
                              else legacy_evidence_field(rendered["text"], row["reasoning"]))
            rows.append({
                "session_id": evaluation["primary_provider_transcript_id"],
                "session_id_match": session_id_match(
                    evaluation["primary_provider_transcript_id"], order),
                "checklist_order": order,
                # Always the canonical string, never a model spelling variant.
                "checklist_item": CHECKLIST_ITEMS[order - 1],
                "status": row["status"], "evidence": evidence_field,
                "severity": SEVERITY.get(row["status"]),
                "rendered_evidence": rendered["text"], "reasoning": row["reasoning"],
                "ai_status": row["ai_status"], "status_source": row["status_source"],
                "evidence_clip_ids": [clip_id for clip_id in rendered["clip_ids"] if clip_id],
                "cue_ids": list(rendered["cue_ids"]),
                "rendered_block_count": rendered["block_count"],
            })
        return rows, engagement_evidence

    def _item7_evidence(self, connection, evaluation) -> str:
        """
        Legacy deterministic Item 7 evidence, from the FROZEN snapshot only.

        Who spoke is listed by transcript speaker label, loudest first, as
        legacy did; who was silent is listed by attendance name, ordered
        deterministically because legacy's raw database order is not
        reproducible.
        """
        participants = self.input_repository.load_participants(
            connection, evaluation["engagement_id"])
        spoke = [row for row in participants if row["participation_status"] == "SPOKE"]
        spoke.sort(key=lambda row: (-(row["gross_spoken_ms"] or 0),
                                    row["speaker_label_raw"] or ""))
        silent = [row["display_name_raw"] for row in participants
                  if row["participation_status"] == "SILENT"]
        return item7_evidence(
            spoke_count=evaluation["spoke_count"],
            attended_count=evaluation["attended_count"],
            engagement_percentage=evaluation["engagement_percentage"],
            spoke_labels=[row["speaker_label_raw"] or row["display_name_raw"]
                          for row in spoke],
            silent_names=silent)

    def _lms_snapshot(self, connection, evaluation, counters) -> dict:
        """
        Reuse the stored snapshot, or capture one.

        The live LMS table is queried ONLY when no snapshot exists for this
        lecture and version, or when an explicit refresh was requested.
        """
        module_raw = evaluation["module"] or evaluation["subject"] or ""
        existing = self.lms_snapshot_repository.find_latest(
            connection, evaluation["lecture_id"], self.lms_snapshot_version)
        if existing is not None and not self.refresh_lms:
            counters["lms_snapshots_reused"] += 1
            members = self.lms_snapshot_repository.load_members(
                connection, existing["snapshot_id"])
            return {**existing, "members": members,
                    "students_payload": legacy_students_payload(members)}

        source = self.lms_source_repository.load_students(connection, module_raw)
        members = source["members"]
        module_normalized = normalize_module(module_raw)
        fingerprint = lms_fingerprint(module_normalized=module_normalized, members=members,
                                      snapshot_version=self.lms_snapshot_version)
        snapshot_id = uuid.uuid5(
            LMS_NAMESPACE,
            f"lecture:{evaluation['lecture_id']}|version:{self.lms_snapshot_version}"
            f"|fingerprint:{fingerprint}")
        snapshot = {
            "snapshot_id": snapshot_id, "lecture_id": evaluation["lecture_id"],
            "lms_snapshot_version": self.lms_snapshot_version,
            "module": module_raw, "module_normalized": module_normalized,
            "source_row_count": source["source_row_count"],
            "student_count": len(members),
            "row_cap_reached": source["source_row_count"] >= LEGACY_ROW_CAP,
            "source_fingerprint": fingerprint,
            "metadata": {"query": "legacy Get LMS Students",
                         "row_cap": LEGACY_ROW_CAP,
                         "lms_snapshot_version": self.lms_snapshot_version},
        }
        written = self.lms_snapshot_repository.upsert(connection, snapshot, members)
        counters["lms_snapshots_created"] += written["created"]
        counters["lms_snapshots_reused"] += written["reused"]
        return {**snapshot, "members": members,
                "students_payload": legacy_students_payload(members)}

    def _legacy_comparison(self, connection, target_date, results) -> dict:
        """READ-ONLY field-by-field comparison with the historical QA rows."""
        sessions = self.legacy_repository.load_sessions(connection, target_date)
        legacy_checklists = self.legacy_repository.load_checklist(connection, target_date)
        by_subject = {normalize_group(row["subject"]): row for row in results}
        classes = {
            "session_id": "DETERMINISTIC", "meeting_id": "DETERMINISTIC",
            "subject": "DETERMINISTIC", "trainer": "DETERMINISTIC",
            "date": "DETERMINISTIC", "duration": "DETERMINISTIC",
            "duration_score": "DETERMINISTIC", "engagement": "DETERMINISTIC",
            "engagement_score": "DETERMINISTIC", "cancelled_session": "DETERMINISTIC",
            "item2": "KNOWN_LEGACY_DEFECT",
            "lms_module": "LIVE_SOURCE_DRIFT_POSSIBLE",
            "lms_students_count": "LIVE_SOURCE_DRIFT_POSSIBLE",
            "met_count": "AI_GENERATED", "partial_count": "AI_GENERATED",
            "not_met_count": "AI_GENERATED",
            "teaching_quality_rating": "AI_GENERATED",
        }
        totals = {key: 0 for key in classes}
        rows = []
        structural = {"checklist_rows": 0, "item_strings": 0, "session_id_match": 0,
                      "status_match": 0, "evidence_exact": 0, "compared": 0}
        for legacy in sessions:
            ours = by_subject.get(normalize_group(legacy["subject"] or ""))
            if ours is None or not ours.get("rendered_session_id"):
                rows.append({"subject": legacy["subject"], "status": "NO_RENDERED_OUTPUT"})
                continue
            stored = _load_rendered(connection, uuid.UUID(ours["rendered_session_id"]))
            legacy_items = legacy_checklists.get(legacy["subject"], {})
            matches = {
                "session_id": stored["session_id"] == legacy["session_id"],
                "meeting_id": stored["meeting_id"] == legacy["meeting_id"],
                "subject": stored["subject"] == legacy["subject"],
                "trainer": stored["trainer"] == legacy["trainer"],
                "date": stored["legacy_date"] == legacy["date"],
                "duration": stored["duration"] == legacy["duration"],
                "duration_score": stored["duration_score"] == legacy["duration_score"],
                "engagement": _decimal_equal(stored["engagement"], legacy["engagement"]),
                "engagement_score": stored["engagement_score"] == legacy["engagement_score"],
                "cancelled_session": _bool_equal(stored["cancelled_session"],
                                                 legacy["cancelled_session"]),
                "lms_module": stored["lms_module"] == legacy["lms_module"],
                "lms_students_count": (stored["lms_students_count"]
                                       == legacy["lms_students_count"]),
                "met_count": stored["met_count"] == legacy["met_count"],
                "partial_count": stored["partial_count"] == legacy["partial_count"],
                "not_met_count": stored["not_met_count"] == legacy["not_met_count"],
                "teaching_quality_rating": (stored["teaching_quality_rating"]
                                            == legacy["teaching_quality_rating"]),
                "item2": (stored["items"].get(2, {}).get("status")
                          == (legacy_items.get(2) or {}).get("status")),
            }
            for key, value in matches.items():
                totals[key] += bool(value)

            structural["compared"] += 1
            structural["checklist_rows"] += len(stored["items"]) == 11
            structural["item_strings"] += all(
                stored["items"][order]["checklist_item"]
                == (legacy_items.get(order) or {}).get("checklist_item", "").strip()
                for order in stored["items"] if legacy_items.get(order))
            structural["session_id_match"] += all(
                stored["items"][order]["session_id_match"]
                == (legacy_items.get(order) or {}).get("session_id_match")
                for order in stored["items"] if legacy_items.get(order))
            status_agree = sum(1 for order in stored["items"]
                               if legacy_items.get(order)
                               and stored["items"][order]["status"]
                               == legacy_items[order]["status"])
            evidence_exact = sum(1 for order in stored["items"]
                                 if legacy_items.get(order)
                                 and stored["items"][order]["evidence"]
                                 == legacy_items[order]["evidence"])
            structural["status_match"] += status_agree
            structural["evidence_exact"] += evidence_exact
            rows.append({
                "subject": legacy["subject"],
                **{f"{key}_match": ("YES" if value else "NO")
                   for key, value in matches.items()},
                "checklist_status_agreement": f"{status_agree} / 11",
                "evidence_exact_match": f"{evidence_exact} / 11",
                "canonical_item2_status": stored["items"].get(2, {}).get("status"),
                "legacy_historical_item2_status": (legacy_items.get(2) or {}).get("status"),
                # Drift against the historical roster, by counts and ids only.
                "lms_comparison": compare_with_legacy(
                    [{"external_learner_id": student.get("ID"),
                      "full_name": student.get("FullName")}
                     for student in ((stored.get("lms_students") or {}).get("students") or [])],
                    legacy["lms_students"], legacy["lms_students_count"]),
                "new_lms_students_count": stored["lms_students_count"],
                "legacy_lms_students_count": legacy["lms_students_count"],
            })
        count = len(sessions)
        return {
            "qa_rows": count,
            "field_classes": classes,
            **{f"{key}_parity": f"{totals[key]} / {count}" for key in classes},
            "checklist_structure": {
                "sessions_compared": structural["compared"],
                "eleven_rows": f"{structural['checklist_rows']} / {count}",
                "item_strings_match": f"{structural['item_strings']} / {count}",
                "session_id_match_format": f"{structural['session_id_match']} / {count}",
                "status_agreement": f"{structural['status_match']} / {count * 11}",
                "evidence_exact_match": f"{structural['evidence_exact']} / {count * 11}",
            },
            "note": "Deterministic fields must match. AI-generated content and live LMS "
                    "data may legitimately differ; Item 2 differs by design.",
            "rows": rows,
        }


def _load_rendered(connection, rendered_session_id) -> dict:
    row = connection.execute("""
    SELECT session_id, meeting_id, subject, trainer, legacy_date, duration, duration_score,
           engagement, engagement_score, met_count, partial_count, not_met_count,
           teaching_quality_rating, cancelled_session, lms_module, lms_students_count,
           lms_students
      FROM public.lecture_qa_rendered_sessions WHERE rendered_session_id = %s
    """, (rendered_session_id,)).fetchone()
    names = ("session_id", "meeting_id", "subject", "trainer", "legacy_date", "duration",
             "duration_score", "engagement", "engagement_score", "met_count",
             "partial_count", "not_met_count", "teaching_quality_rating",
             "cancelled_session", "lms_module", "lms_students_count", "lms_students")
    stored = dict(zip(names, row)) if row else {name: None for name in names}
    stored["items"] = {}
    for item in connection.execute("""
    SELECT checklist_order, checklist_item, status, session_id_match, evidence
      FROM public.lecture_qa_rendered_checklist_items
     WHERE rendered_session_id = %s ORDER BY checklist_order
    """, (rendered_session_id,)).fetchall():
        stored["items"][item[0]] = {"checklist_item": item[1], "status": item[2],
                                    "session_id_match": item[3], "evidence": item[4]}
    return stored


def _decimal_equal(left, right) -> bool:
    if left is None or right is None:
        return False
    return Decimal(str(left)) == Decimal(str(right))


def _bool_equal(left, right) -> bool:
    def coerce(value):
        if isinstance(value, bool):
            return value
        if value is None:
            return None
        return str(value).strip().lower() in ("true", "t", "yes", "1")
    return coerce(left) == coerce(right)
