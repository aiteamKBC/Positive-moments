"""
Phase 2A persistence: raw transcript artifacts and their content versions.

Writes are confined to public.lecture_transcript_artifacts and
public.lecture_transcript_artifact_contents. Nothing here reads or writes any
legacy QA, media, or Positive Clips table, and public.qa_doctors_transcripts is
untouched.
"""
import json
import uuid

from app.common.errors import DATABASE_ERROR, PlatformError


CONTENT_NAMESPACE = uuid.UUID("b1c7d0ae-4a55-4a1d-9f3e-2d8b6c5a9e07")
CANDIDATE_NAMESPACE = uuid.UUID("2c9f7b16-51a4-4c3f-9e08-7d6b4a2f1c85")


class TranscriptArtifactRepository:
    def upsert(self, connection, artifact_id, lecture_id, *, provider, provider_transcript_id,
               meeting_id, meeting_lookup_user_id, provider_call_id, content_correlation_id,
               provider_created_at, provider_end_at, artifact_status, metadata) -> str:
        """
        Register one RAW PROVIDER artifact and link it to the lecture that saw it.

        The artifact row is keyed by provider identity, so a recurring-series
        transcript visible to several lectures is stored - bytes included -
        exactly once. `lecture_id` records visibility in
        lecture_transcript_candidates. Re-running only refreshes last_seen_at
        and provider metadata; it never touches stored content.
        """
        try:
            row = connection.execute("""
            INSERT INTO public.lecture_transcript_artifacts (
                artifact_id, provider, provider_transcript_id,
                meeting_id, meeting_lookup_user_id, provider_call_id, content_correlation_id,
                provider_created_at, provider_end_at, artifact_status, metadata
            ) VALUES (
                %(artifact_id)s, %(provider)s, %(provider_transcript_id)s,
                %(meeting_id)s, %(meeting_lookup_user_id)s, %(provider_call_id)s,
                %(content_correlation_id)s, %(provider_created_at)s, %(provider_end_at)s,
                %(artifact_status)s, %(metadata)s::jsonb
            )
            ON CONFLICT (artifact_id) DO UPDATE SET
                provider_call_id = EXCLUDED.provider_call_id,
                content_correlation_id = EXCLUDED.content_correlation_id,
                provider_created_at = EXCLUDED.provider_created_at,
                provider_end_at = EXCLUDED.provider_end_at,
                meeting_lookup_user_id = EXCLUDED.meeting_lookup_user_id,
                metadata = EXCLUDED.metadata,
                last_seen_at = now(), updated_at = now()
            RETURNING (xmax = 0) AS inserted
            """, {
                "artifact_id": artifact_id, "provider": provider,
                "provider_transcript_id": provider_transcript_id, "meeting_id": meeting_id,
                "meeting_lookup_user_id": meeting_lookup_user_id,
                "provider_call_id": provider_call_id,
                "content_correlation_id": content_correlation_id,
                "provider_created_at": provider_created_at, "provider_end_at": provider_end_at,
                "artifact_status": artifact_status, "metadata": json.dumps(metadata or {}),
            }).fetchone()
            connection.execute(
                "INSERT INTO public.lecture_transcript_candidates "
                "(candidate_id, lecture_id, artifact_id, meeting_id, meeting_lookup_user_id) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (lecture_id, artifact_id) DO UPDATE SET last_seen_at = now()",
                (uuid.uuid5(CANDIDATE_NAMESPACE, f"{lecture_id}\0{artifact_id}"),
                 lecture_id, artifact_id, meeting_id, meeting_lookup_user_id),
            )
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "transcript artifact upsert failed") from exc
        return "created" if row[0] else "existing"

    def current_content_hash(self, connection, artifact_id) -> str | None:
        try:
            row = connection.execute(
                "SELECT content_sha256 FROM public.lecture_transcript_artifacts WHERE artifact_id = %s",
                (artifact_id,),
            ).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "transcript artifact lookup failed") from exc
        return row[0] if row else None

    def store_content(self, connection, artifact_id, *, raw_text, content_sha256,
                      content_bytes, content_format, speaker_attribution) -> dict:
        """
        Append-only content storage.

        Identical bytes reuse the existing version - no duplicate evidence.
        Changed bytes append a NEW version; earlier versions are never modified
        or deleted, which is the whole provenance guarantee.
        """
        try:
            existing = connection.execute(
                "SELECT content_id, version_number FROM public.lecture_transcript_artifact_contents "
                "WHERE artifact_id = %s AND content_sha256 = %s",
                (artifact_id, content_sha256),
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE public.lecture_transcript_artifact_contents "
                    "SET last_fetched_at = now() WHERE content_id = %s",
                    (existing[0],),
                )
                version_number, created = existing[1], False
            else:
                version_number = (connection.execute(
                    "SELECT coalesce(max(version_number), 0) + 1 "
                    "FROM public.lecture_transcript_artifact_contents WHERE artifact_id = %s",
                    (artifact_id,),
                ).fetchone()[0])
                connection.execute("""
                INSERT INTO public.lecture_transcript_artifact_contents (
                    content_id, artifact_id, version_number, content_format,
                    speaker_attribution, raw_content, content_sha256, content_bytes
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    uuid.uuid5(CONTENT_NAMESPACE, f"{artifact_id}\0{content_sha256}"),
                    artifact_id, version_number, content_format, speaker_attribution,
                    raw_text, content_sha256, content_bytes,
                ))
                created = True
            connection.execute("""
            UPDATE public.lecture_transcript_artifacts
               SET content_format = %s, speaker_attribution = %s, content_sha256 = %s,
                   content_bytes = %s, content_version = %s, artifact_status = %s,
                   content_fetched_at = now(), last_seen_at = now(), updated_at = now()
             WHERE artifact_id = %s
            """, (
                content_format, speaker_attribution, content_sha256, content_bytes,
                version_number,
                "CONTENT_STORED" if speaker_attribution else "CONTENT_STORED_NO_SPEAKER_ATTRIBUTION",
                artifact_id,
            ))
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "transcript content storage failed") from exc
        return {"version_number": version_number, "version_created": created}

    def mark_content_failure(self, connection, artifact_id, artifact_status: str, reason: str | None) -> None:
        """Record a content failure without inventing a successful transcript state."""
        try:
            connection.execute("""
            UPDATE public.lecture_transcript_artifacts
               SET artifact_status = %s, last_seen_at = now(), updated_at = now(),
                   metadata = metadata || jsonb_build_object('content_fetch_reason', %s::text)
             WHERE artifact_id = %s
            """, (artifact_status, reason, artifact_id))
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "transcript artifact status update failed") from exc

    def count_for_date(self, connection, target_date) -> int:
        try:
            return connection.execute("""
            SELECT count(DISTINCT c.artifact_id)
              FROM public.lecture_transcript_candidates c
              JOIN public.lecture_sessions l ON l.lecture_id = c.lecture_id
             WHERE l.session_date = %s
            """, (target_date,)).fetchone()[0]
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "transcript artifact count failed") from exc
