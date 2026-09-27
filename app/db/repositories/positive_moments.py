"""
Persistence for Positive Moment Evidence Intelligence (migration 023).

Reads: canonical cues and the deterministic trainer of ONE document - the
document the pipeline resolver names as current - and the lecture's legacy
Positive Clips V5 result. No Graph, no transcript download.

Writes: positive_moment_analyses and positive_moments only. Idempotent: an
analysis is keyed (lecture_id, input_fingerprint) and a moment (analysis_id,
start_cue, end_cue), so re-running over the same inputs writes nothing new.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from app.common.errors import DATABASE_ERROR, PlatformError
from app.positive_moments import policy as p
from app.positive_moments.models import CanonicalTranscript, Cue

CUES = """
SELECT cue_index, start_ms, end_ms, speaker_label_raw, text
  FROM public.lecture_transcript_cues
 WHERE document_id = %s
 ORDER BY cue_index
"""

DOCUMENT = """
SELECT d.document_id, d.lecture_id, d.source_fingerprint, l.subject
  FROM public.lecture_transcript_documents d
  JOIN public.lecture_sessions l ON l.lecture_id = d.lecture_id
 WHERE d.document_id = %s
"""

# The deterministic trainer (VTT_TOP_SPEAKER). A stored TRAINER_CANDIDATE role
# for THIS document wins; without one, the same rule is applied to the speaker
# inventory: most gross spoken milliseconds, label as the tie-break.
TRAINER_ROLE = """
SELECT sp.speaker_label_raw
  FROM public.lecture_transcript_speaker_roles r
  JOIN public.lecture_transcript_speakers sp ON sp.speaker_id = r.speaker_id
 WHERE sp.document_id = %s AND r.role = 'TRAINER_CANDIDATE'
 ORDER BY r.created_at DESC, sp.speaker_label_raw
 LIMIT 1
"""
TOP_SPEAKER = """
SELECT speaker_label_raw FROM public.lecture_transcript_speakers
 WHERE document_id = %s
 ORDER BY gross_spoken_ms DESC, speaker_label_raw
 LIMIT 1
"""

LEGACY_CLIPS = """
SELECT clips_analysis_completeness, positive_clips
  FROM public.qa_doctors_sessions
 WHERE session_id = %s
"""

ANALYSIS_FIELDS = ("analysis_id", "lecture_id", "document_id", "document_source_fingerprint",
                   "transcript_fingerprint", "analysis_policy_version", "input_fingerprint",
                   "source", "status", "candidate_count", "structurally_valid_count",
                   "accepted_count", "rejection_summary", "model_metadata",
                   "legacy_source_fingerprint", "error_code", "error_message",
                   "started_at", "completed_at", "created_at", "updated_at")
ANALYSIS_COLUMNS = ", ".join(ANALYSIS_FIELDS)

MOMENT_FIELDS = ("moment_id", "analysis_id", "lecture_id", "document_id",
                 "transcript_fingerprint", "analysis_policy_version", "moment_fingerprint",
                 "moment_index", "start_cue", "end_cue", "evidence_start_ms",
                 "evidence_end_ms", "all_speakers", "positive_speakers", "trainer_included",
                 "conversation_type", "category", "exact_quote", "positive_quote",
                 "dialogue", "reason", "selector_confidence", "verifier_verdict",
                 "verifier_confidence", "source", "status", "created_at", "updated_at")
MOMENT_COLUMNS = ", ".join(MOMENT_FIELDS)


def _db(message):
    return PlatformError(DATABASE_ERROR, message)


def _dicts(rows, fields):
    return [dict(zip(fields, row)) for row in rows]


def analysis_fingerprint(*, document_id, transcript_fingerprint, source,
                         legacy_fingerprint=None, policy_version=p.ANALYSIS_POLICY_VERSION) -> str:
    import hashlib
    raw = "|".join([policy_version, str(document_id), transcript_fingerprint, source,
                    legacy_fingerprint or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class PositiveMomentRepository:

    # -- reads ------------------------------------------------------------------

    def transcript(self, connection, document_id) -> CanonicalTranscript | None:
        try:
            document = connection.execute(DOCUMENT, (str(document_id),)).fetchone()
            if document is None:
                return None
            cues = tuple(Cue(int(r[0]), int(r[1]), int(r[2]), r[3], r[4] or "")
                         for r in connection.execute(CUES, (str(document_id),)).fetchall())
            trainer = connection.execute(TRAINER_ROLE, (str(document_id),)).fetchone()
            if trainer is None:
                trainer = connection.execute(TOP_SPEAKER, (str(document_id),)).fetchone()
        except Exception as exc:
            raise _db("canonical transcript read failed") from exc
        trainer_label = trainer[0] if trainer else _top_speaker(cues)
        return CanonicalTranscript(lecture_id=str(document[1]), document_id=str(document[0]),
                                   document_source_fingerprint=document[2] or "",
                                   cues=cues, trainer_speaker=trainer_label,
                                   subject=document[3])

    def legacy_result(self, connection, legacy_session_id) -> tuple:
        if not legacy_session_id:
            return None, None
        try:
            row = connection.execute(LEGACY_CLIPS, (legacy_session_id,)).fetchone()
        except Exception as exc:
            raise _db("legacy positive clips read failed") from exc
        return (row[0], row[1]) if row else (None, None)

    def current_analysis(self, connection, lecture_id, *, document_id,
                         transcript_fingerprint) -> dict | None:
        """The newest settled analysis of exactly this transcript under this policy."""
        rows = self._query(
            f"SELECT {ANALYSIS_COLUMNS} FROM public.positive_moment_analyses "
            "WHERE lecture_id = %s AND document_id = %s AND transcript_fingerprint = %s "
            "  AND analysis_policy_version = %s "
            "  AND status IN ('MOMENTS_FOUND', 'NO_POSITIVE_MOMENTS', 'REVIEW_REQUIRED') "
            "ORDER BY created_at DESC LIMIT 1",
            (str(lecture_id), str(document_id), transcript_fingerprint,
             p.ANALYSIS_POLICY_VERSION), ANALYSIS_FIELDS, connection=connection)
        return rows[0] if rows else None

    def latest_analysis(self, connection, lecture_id) -> dict | None:
        rows = self._query(
            f"SELECT {ANALYSIS_COLUMNS} FROM public.positive_moment_analyses "
            "WHERE lecture_id = %s ORDER BY created_at DESC LIMIT 1",
            (str(lecture_id),), ANALYSIS_FIELDS, connection=connection)
        return rows[0] if rows else None

    def moments(self, connection, analysis_id) -> list[dict]:
        return self._query(
            f"SELECT {MOMENT_COLUMNS} FROM public.positive_moments "
            "WHERE analysis_id = %s ORDER BY moment_index",
            (str(analysis_id),), MOMENT_FIELDS, connection=connection)

    def moment(self, connection, moment_id) -> dict | None:
        rows = self._query(f"SELECT {MOMENT_COLUMNS} FROM public.positive_moments "
                           "WHERE moment_id = %s", (str(moment_id),), MOMENT_FIELDS,
                           connection=connection)
        return rows[0] if rows else None

    def analysis_for(self, connection, lecture_id, input_fingerprint):
        rows = self._query(
            f"SELECT {ANALYSIS_COLUMNS} FROM public.positive_moment_analyses "
            "WHERE lecture_id = %s AND input_fingerprint = %s",
            (str(lecture_id), input_fingerprint), ANALYSIS_FIELDS, connection=connection)
        return rows[0] if rows else None

    @staticmethod
    def _query(sql, params, fields, *, connection):
        try:
            return _dicts(connection.execute(sql, params).fetchall(), fields)
        except Exception as exc:
            raise _db("positive moment read failed") from exc

    # -- writes -----------------------------------------------------------------

    def save_analysis(self, connection, *, transcript: CanonicalTranscript, source: str,
                      status: str, moments: list, candidate_count: int,
                      structurally_valid_count: int, rejection_summary: dict,
                      model_metadata: dict, legacy_fingerprint=None, error_code=None,
                      error_message=None) -> dict:
        """
        Persist one settled analysis and its moments. Returns the analysis row.

        If an analysis with the same input fingerprint already exists it is
        returned untouched - unless it FAILED, in which case this attempt
        replaces its outcome (a retry is the same analysis, not a new one).
        """
        fingerprint = analysis_fingerprint(
            document_id=transcript.document_id, transcript_fingerprint=transcript.fingerprint,
            source=source, legacy_fingerprint=legacy_fingerprint)
        existing = self.analysis_for(connection, transcript.lecture_id, fingerprint)
        if existing and existing["status"] != "FAILED":
            return existing
        now = datetime.now(timezone.utc)
        analysis_id = existing["analysis_id"] if existing else uuid.uuid4()
        params = {
            "analysis_id": analysis_id, "lecture_id": transcript.lecture_id,
            "document_id": transcript.document_id,
            "document_source_fingerprint": transcript.document_source_fingerprint,
            "transcript_fingerprint": transcript.fingerprint,
            "policy": p.ANALYSIS_POLICY_VERSION, "input_fingerprint": fingerprint,
            "source": source, "status": status, "candidate_count": candidate_count,
            "valid": structurally_valid_count, "accepted": len(moments),
            "rejections": Jsonb(dict(rejection_summary or {})),
            "metadata": Jsonb({**(model_metadata or {}), "policy": p.describe()}),
            "legacy": legacy_fingerprint, "error_code": error_code,
            "error_message": (error_message or "")[:500] or None, "now": now,
        }
        try:
            connection.execute("""
                INSERT INTO public.positive_moment_analyses (
                    analysis_id, lecture_id, document_id, document_source_fingerprint,
                    transcript_fingerprint, analysis_policy_version, input_fingerprint,
                    source, status, candidate_count, structurally_valid_count,
                    accepted_count, rejection_summary, model_metadata,
                    legacy_source_fingerprint, error_code, error_message, started_at,
                    completed_at, updated_at)
                VALUES (%(analysis_id)s, %(lecture_id)s, %(document_id)s,
                        %(document_source_fingerprint)s, %(transcript_fingerprint)s,
                        %(policy)s, %(input_fingerprint)s, %(source)s, %(status)s,
                        %(candidate_count)s, %(valid)s, %(accepted)s, %(rejections)s,
                        %(metadata)s, %(legacy)s, %(error_code)s, %(error_message)s,
                        %(now)s, %(now)s, %(now)s)
                ON CONFLICT (lecture_id, input_fingerprint) DO UPDATE SET
                    status = EXCLUDED.status, candidate_count = EXCLUDED.candidate_count,
                    structurally_valid_count = EXCLUDED.structurally_valid_count,
                    accepted_count = EXCLUDED.accepted_count,
                    rejection_summary = EXCLUDED.rejection_summary,
                    model_metadata = EXCLUDED.model_metadata,
                    error_code = EXCLUDED.error_code, error_message = EXCLUDED.error_message,
                    completed_at = EXCLUDED.completed_at, updated_at = EXCLUDED.updated_at
                 WHERE public.positive_moment_analyses.status = 'FAILED'
            """, params)
            for index, moment in enumerate(moments, start=1):
                self._insert_moment(connection, analysis_id, transcript, index, moment)
        except Exception as exc:
            raise _db("positive moment analysis write failed") from exc
        return self.analysis_for(connection, transcript.lecture_id, fingerprint)

    def _insert_moment(self, connection, analysis_id, transcript, index, moment) -> None:
        connection.execute("""
            INSERT INTO public.positive_moments (
                moment_id, analysis_id, lecture_id, document_id, transcript_fingerprint,
                analysis_policy_version, moment_fingerprint, moment_index, start_cue,
                end_cue, evidence_start_ms, evidence_end_ms, all_speakers,
                positive_speakers, trainer_included, conversation_type, category,
                exact_quote, positive_quote, dialogue, reason, selector_confidence,
                verifier_verdict, verifier_confidence, source)
            VALUES (%(moment_id)s, %(analysis_id)s, %(lecture_id)s, %(document_id)s,
                    %(transcript_fingerprint)s, %(policy)s, %(fingerprint)s, %(index)s,
                    %(start_cue)s, %(end_cue)s, %(start_ms)s, %(end_ms)s, %(all_speakers)s,
                    %(positive_speakers)s, %(trainer_included)s, %(conversation_type)s,
                    %(category)s, %(exact_quote)s, %(positive_quote)s, %(dialogue)s,
                    %(reason)s, %(selector_confidence)s, %(verifier_verdict)s,
                    %(verifier_confidence)s, %(source)s)
            ON CONFLICT DO NOTHING
        """, {
            "moment_id": uuid.uuid4(), "analysis_id": analysis_id,
            "lecture_id": transcript.lecture_id, "document_id": transcript.document_id,
            "transcript_fingerprint": transcript.fingerprint,
            "policy": p.ANALYSIS_POLICY_VERSION,
            "fingerprint": moment.fingerprint(
                document_id=transcript.document_id,
                transcript_fingerprint=transcript.fingerprint,
                policy_version=p.ANALYSIS_POLICY_VERSION),
            "index": index, "start_cue": moment.start_cue, "end_cue": moment.end_cue,
            "start_ms": moment.evidence_start_ms, "end_ms": moment.evidence_end_ms,
            "all_speakers": Jsonb(moment.all_speakers),
            "positive_speakers": Jsonb(moment.positive_speakers),
            "trainer_included": moment.trainer_included,
            "conversation_type": moment.conversation_type, "category": moment.category,
            "exact_quote": moment.exact_quote, "positive_quote": moment.positive_quote,
            "dialogue": Jsonb(moment.dialogue), "reason": moment.reason,
            "selector_confidence": moment.selector_confidence,
            "verifier_verdict": moment.verifier_verdict,
            "verifier_confidence": moment.verifier_confidence, "source": moment.source,
        })


def _top_speaker(cues) -> str | None:
    spoken: dict = {}
    for cue in cues:
        if cue.speaker:
            spoken[cue.speaker] = spoken.get(cue.speaker, 0) + max(cue.end_ms - cue.start_ms, 0)
    if not spoken:
        return None
    return sorted(spoken.items(), key=lambda item: (-item[1], item[0]))[0][0]
