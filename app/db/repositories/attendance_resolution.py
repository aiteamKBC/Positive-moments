"""
Phase 2C3 persistence: attendance snapshots, person resolution, speaker roles.

READ-ONLY sources: public.kbc_attendance (external), lecture_sessions,
lecture_transcript_documents, lecture_transcript_speakers, and
qa_doctors_sessions for the legacy diagnostic.

WRITES ONLY: lecture_attendance_snapshots, lecture_attendance_snapshot_members,
lecture_transcript_speaker_identities, lecture_transcript_speaker_roles,
lecture_speaker_resolution_runs.

The external attendance table is never inserted into, updated, deleted from,
indexed or triggered. Every statement that touches it is a plain SELECT.
"""
import json
import uuid

from app.common.errors import DATABASE_ERROR, PlatformError


# Speaker inventory for one date, joined to its lecture. The inventory is read
# only: no column of it is ever written by this phase.
LOAD_SPEAKERS = """
SELECT l.lecture_id, l.subject, l.module, l.session_date,
       d.document_id, s.speaker_id, s.speaker_label_raw,
       s.speaker_label_normalized, s.gross_spoken_ms, s.first_cue_index, s.cue_count
  FROM public.lecture_transcript_speakers s
  JOIN public.lecture_transcript_documents d ON d.document_id = s.document_id
  JOIN public.lecture_sessions l ON l.lecture_id = d.lecture_id
 WHERE l.session_date = %s
   AND s.speaker_inventory_version = %s
   AND d.parser_version = %s
 ORDER BY l.scheduled_start, l.subject, s.gross_spoken_ms DESC, s.speaker_label_raw
"""

# Legacy Attendance (DB-based) subflow contract, ported exactly:
#   exact lecture date + exact normalized module + Attendance = 1.
# Nothing broader. No roster is assembled from any other table.
# attendance_status is selected, never filtered here: the makeup rule is a
# versioned roster decision applied in app.attendance.roster, so the v1 rule
# stays reproducible from the same query.
LOAD_ATTENDANCE = """
SELECT a."ID", a."FullName", a."Email", a."Attendance", a.module, a.attendance_status
  FROM public.kbc_attendance a
 WHERE a.date = %s
   AND btrim(regexp_replace(lower(a.module), '\\s+', ' ', 'g')) = %s
   AND a."Attendance" = 1
 ORDER BY a."ID"
"""


# The lecture-scoped variant. A SEPARATE statement, not an extra predicate:
# the scope is part of the contract, so leaving an argument unset must not be
# able to reach the date-wide query. There is no session_date predicate here.
LOAD_SPEAKERS_FOR_LECTURE = LOAD_SPEAKERS.replace(
    " WHERE l.session_date = %s", " WHERE l.lecture_id = %s")


# The SAME (date, module) contract with NO attendance filter. Read-only, and
# used for exactly one purpose: telling "the source never mentioned this
# lecture" apart from "the source mentioned it only to record absences". It
# never contributes a roster member and never touches the fingerprint.
COUNT_ATTENDANCE_ANY_STATUS = """
SELECT count(*)::int,
       count(*) FILTER (WHERE a."Attendance" = 1)::int
  FROM public.kbc_attendance a
 WHERE a.date = %s
   AND btrim(regexp_replace(lower(a.module), '\\s+', ' ', 'g')) = %s
"""


class AttendanceSourceRepository:
    """READ-ONLY gateway to the externally owned public.kbc_attendance."""

    def load_present_rows(self, connection, session_date, module_normalized) -> list[dict]:
        try:
            rows = connection.execute(
                LOAD_ATTENDANCE, (session_date, module_normalized)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "attendance source query failed") from exc
        return [
            # "ID" is a per-learner identifier; it repeats across dates.
            {"learner_id": row[0], "full_name": row[1], "email": row[2],
             "attendance_flag": row[3], "module": row[4], "attendance_status": row[5]}
            for row in rows
        ]

    def count_rows_any_status(self, connection, session_date,
                              module_normalized) -> dict:
        """
        How many rows the source holds for this lecture, regardless of flag.

        Still strictly read-only, still the same query contract. The roster
        itself is unchanged: this count exists only so an empty roster can be
        read honestly.
        """
        try:
            row = connection.execute(
                COUNT_ATTENDANCE_ANY_STATUS,
                (session_date, module_normalized)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR,
                                "attendance source count failed") from exc
        return {"source_rows_any_status": row[0], "source_rows_present": row[1]}


class SpeakerInventoryReadRepository:
    """READ-ONLY view of the Phase 2C2 inventory, grouped per lecture."""

    def load_day(self, connection, target_date, inventory_version, parser_version) -> list[dict]:
        try:
            rows = connection.execute(
                LOAD_SPEAKERS, (target_date, inventory_version, parser_version)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "speaker inventory query failed") from exc
        lectures: dict = {}
        order: list = []
        for row in rows:
            lecture_id = row[0]
            if lecture_id not in lectures:
                lectures[lecture_id] = {
                    "lecture_id": lecture_id, "subject": row[1], "module": row[2],
                    "session_date": row[3], "document_id": row[4], "speakers": [],
                }
                order.append(lecture_id)
            lectures[lecture_id]["speakers"].append({
                "speaker_id": row[5], "speaker_label_raw": row[6],
                "speaker_label_normalized": row[7], "gross_spoken_ms": int(row[8]),
                "first_cue_index": row[9], "cue_count": row[10],
            })
        return [lectures[lecture_id] for lecture_id in order]

    def load_lecture(self, connection, lecture_id, inventory_version,
                     parser_version) -> dict | None:
        """EXACTLY one lecture's speaker inventory. Never widens to the date."""
        try:
            rows = connection.execute(
                LOAD_SPEAKERS_FOR_LECTURE,
                (lecture_id, inventory_version, parser_version)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR,
                                "lecture speaker inventory query failed") from exc
        if not rows:
            return None
        lectures = self._group(rows)
        if len(lectures) != 1:
            raise PlatformError(DATABASE_ERROR,
                                f"lecture scope leaked to {len(lectures)} lectures")
        return lectures[0]

    @staticmethod
    def _group(rows) -> list[dict]:
        lectures: dict = {}
        order: list = []
        for row in rows:
            lecture_id = row[0]
            if lecture_id not in lectures:
                lectures[lecture_id] = {
                    "lecture_id": lecture_id, "subject": row[1], "module": row[2],
                    "session_date": row[3], "document_id": row[4], "speakers": [],
                }
                order.append(lecture_id)
            lectures[lecture_id]["speakers"].append({
                "speaker_id": row[5], "speaker_label_raw": row[6],
                "speaker_label_normalized": row[7], "gross_spoken_ms": int(row[8]),
                "first_cue_index": row[9], "cue_count": row[10],
            })
        return [lectures[lecture_id] for lecture_id in order]


class AttendanceSnapshotRepository:
    def find(self, connection, snapshot_id):
        try:
            row = connection.execute(
                "SELECT snapshot_id FROM public.lecture_attendance_snapshots "
                " WHERE snapshot_id = %s", (snapshot_id,)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "snapshot lookup failed") from exc
        return row[0] if row else None

    def upsert(self, connection, snapshot, members) -> dict:
        """
        Insert a snapshot and its members, or recognise an identical one.

        The snapshot id already contains the roster fingerprint, so an
        unchanged roster resolves to the same row and a changed roster creates
        a new one. Existing snapshots are never rewritten - that is the whole
        point of freezing them.
        """
        existing = self.find(connection, snapshot["snapshot_id"])
        if existing is not None:
            return {"created": 0, "reused": 1, "members_written": 0}
        try:
            connection.execute("""
            INSERT INTO public.lecture_attendance_snapshots (
                snapshot_id, lecture_id, attendance_resolution_version,
                session_date, module, module_normalized,
                source_row_count, present_row_count, excluded_bot_count,
                excluded_no_name_count, deduplicated_count, effective_member_count,
                source_fingerprint, bot_filter_version, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (snapshot_id) DO NOTHING
            """, (
                snapshot["snapshot_id"], snapshot["lecture_id"],
                snapshot["attendance_resolution_version"], snapshot["session_date"],
                snapshot["module"], snapshot["module_normalized"],
                snapshot["source_row_count"], snapshot["present_row_count"],
                snapshot["excluded_bot_count"], snapshot["excluded_no_name_count"],
                snapshot["deduplicated_count"], snapshot["effective_member_count"],
                snapshot["source_fingerprint"], snapshot["bot_filter_version"],
                json.dumps(snapshot.get("metadata", {}), default=str)))
            if members:
                with connection.cursor() as cursor:
                    cursor.executemany("""
                    INSERT INTO public.lecture_attendance_snapshot_members (
                        member_id, snapshot_id, external_person_id, display_name_raw,
                        display_name_normalized, email_sha256, dedup_key, attendance_flag
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (member_id) DO NOTHING
                    """, [
                        (member["member_id"], snapshot["snapshot_id"],
                         member["external_person_id"], member["display_name_raw"],
                         member["display_name_normalized"], member["email_sha256"],
                         member["dedup_key"], member["attendance_flag"])
                        for member in members
                    ])
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "attendance snapshot write failed") from exc
        return {"created": 1, "reused": 0, "members_written": len(members)}


class SpeakerIdentityRepository:
    def existing_ids(self, connection, snapshot_id, resolver_version) -> set:
        try:
            rows = connection.execute(
                "SELECT resolution_id FROM public.lecture_transcript_speaker_identities "
                " WHERE attendance_snapshot_id = %s AND resolver_version = %s",
                (snapshot_id, resolver_version)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "resolution lookup failed") from exc
        return {row[0] for row in rows}

    def upsert(self, connection, snapshot_id, resolver_version, resolutions) -> dict:
        before = self.existing_ids(connection, snapshot_id, resolver_version)
        incoming = {row["resolution_id"] for row in resolutions}
        if not resolutions:
            return {"created": 0, "updated": 0}
        try:
            with connection.cursor() as cursor:
                cursor.executemany("""
                INSERT INTO public.lecture_transcript_speaker_identities (
                    resolution_id, speaker_id, attendance_snapshot_id, resolver_version,
                    resolution_status, matched_source, matched_member_id, matched_person_id,
                    match_method, match_score, candidate_count, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (resolution_id) DO UPDATE SET
                    resolution_status = EXCLUDED.resolution_status,
                    matched_source = EXCLUDED.matched_source,
                    matched_member_id = EXCLUDED.matched_member_id,
                    matched_person_id = EXCLUDED.matched_person_id,
                    match_method = EXCLUDED.match_method,
                    match_score = EXCLUDED.match_score,
                    candidate_count = EXCLUDED.candidate_count,
                    metadata = EXCLUDED.metadata, updated_at = now()
                """, [
                    (row["resolution_id"], row["speaker_id"], snapshot_id, resolver_version,
                     row["resolution_status"], row["matched_source"], row["matched_member_id"],
                     row["matched_person_id"], row["match_method"], row["match_score"],
                     row["candidate_count"], json.dumps(row.get("metadata", {}), default=str))
                    for row in resolutions
                ])
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "speaker resolution write failed") from exc
        created = len(incoming - before)
        return {"created": created, "updated": len(incoming) - created}


class SpeakerRoleRepository:
    def existing_ids(self, connection, snapshot_id, role_algorithm_version,
                     resolver_version) -> set:
        try:
            rows = connection.execute(
                "SELECT role_id FROM public.lecture_transcript_speaker_roles "
                " WHERE attendance_snapshot_id = %s AND role_algorithm_version = %s "
                "   AND resolver_version = %s",
                (snapshot_id, role_algorithm_version, resolver_version)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "role lookup failed") from exc
        return {row[0] for row in rows}

    def upsert(self, connection, snapshot_id, role_algorithm_version, resolver_version,
               roles) -> dict:
        before = self.existing_ids(
            connection, snapshot_id, role_algorithm_version, resolver_version)
        incoming = {row["role_id"] for row in roles}
        if not roles:
            return {"created": 0, "updated": 0}
        try:
            with connection.cursor() as cursor:
                cursor.executemany("""
                INSERT INTO public.lecture_transcript_speaker_roles (
                    role_id, speaker_id, role_algorithm_version, resolver_version,
                    attendance_snapshot_id, role, role_source, role_rank, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (role_id) DO UPDATE SET
                    role = EXCLUDED.role,
                    role_source = EXCLUDED.role_source,
                    role_rank = EXCLUDED.role_rank,
                    metadata = EXCLUDED.metadata, updated_at = now()
                """, [
                    (row["role_id"], row["speaker_id"], role_algorithm_version,
                     resolver_version, snapshot_id, row["role"], row["role_source"],
                     row["role_rank"], json.dumps(row.get("metadata", {}), default=str))
                    for row in roles
                ])
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "speaker role write failed") from exc
        created = len(incoming - before)
        return {"created": created, "updated": len(incoming) - created}


class SpeakerResolutionRunRepository:
    COUNTERS = (
        "lectures_considered", "snapshots_created", "snapshots_reused",
        "snapshot_members_written", "speakers_considered", "resolutions_created",
        "resolutions_updated", "roles_created", "roles_updated", "exact_matches",
        "normalized_exact_matches", "legacy_fuzzy_matches", "ambiguous_matches",
        "unmatched_speakers", "error_count",
    )

    def start(self, connection, target_date, versions, mode="SHADOW") -> uuid.UUID:
        run_id = uuid.uuid4()
        try:
            connection.execute(
                "INSERT INTO public.lecture_speaker_resolution_runs "
                "(run_id, target_date, mode, status, attendance_resolution_version, "
                " resolver_version, role_algorithm_version) "
                "VALUES (%s, %s, %s, 'RUNNING', %s, %s, %s)",
                (run_id, target_date, mode, versions["attendance_resolution_version"],
                 versions["resolver_version"], versions["role_algorithm_version"]))
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "could not create resolution run") from exc
        return run_id

    def complete(self, connection, run_id, summary) -> None:
        assignments = ", ".join(f"{name} = %s" for name in self.COUNTERS)
        values = [int(summary.get(name, 0)) for name in self.COUNTERS]
        try:
            connection.execute(
                "UPDATE public.lecture_speaker_resolution_runs "
                f"SET completed_at = now(), status = %s, {assignments}, metadata = %s::jsonb "
                "WHERE run_id = %s",
                [summary["status"], *values,
                 json.dumps(summary.get("metadata", {}), default=str), run_id])
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "could not complete resolution run") from exc
