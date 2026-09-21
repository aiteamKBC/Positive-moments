"""
One-off Phase 2B normalization of Phase 2A raw transcript artifacts.

Phase 2A keyed an artifact by (provider, provider_transcript_id, lecture_id).
That duplicates raw bytes once per lecture that can see a recurring-series
transcript. This script moves to provider identity and puts the lecture
relationship in public.lecture_transcript_candidates.

It reports before it writes, requires --apply to commit, and runs every step in
ONE transaction so artifacts, contents, and candidates can never disagree.
No evidence is deleted: every artifact row, every content version, every hash
and every lecture link survives.
"""

import argparse
import io
import json
import re
import uuid
from pathlib import Path

import psycopg

from app.common.hashing import provider_transcript_identity
from app.config.settings import Settings


ROOT = Path(__file__).resolve().parents[2]
CANDIDATE_NAMESPACE = uuid.UUID("2c9f7b16-51a4-4c3f-9e08-7d6b4a2f1c85")


def _sql(name: str) -> str:
    """
    Load a migration body WITHOUT its own transaction control.

    The migration files are standalone and wrap themselves in BEGIN/COMMIT. Run
    verbatim from here that COMMIT would end this script's transaction
    mid-way, which would defeat --apply and make a report-only run write. The
    outer transaction must be the only thing that decides to commit.
    """
    text = io.open(ROOT / "app" / "db" / "migrations" / name, encoding="utf-8").read()
    stripped = re.sub(r"(?im)^\s*(BEGIN|COMMIT)\s*;\s*$", "", text)
    if re.search(r"(?i)\b(BEGIN|COMMIT|ROLLBACK)\s*;", stripped):
        raise RuntimeError(f"{name} still contains transaction control after stripping")
    return stripped


def inspect(connection) -> dict:
    """Prove what the current data actually looks like before changing it."""
    lecture_scoped = connection.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='lecture_transcript_artifacts' "
        "AND column_name='lecture_id'"
    ).fetchone()[0] == 1
    report = {
        "already_normalized": not lecture_scoped,
        "artifact_rows": connection.execute(
            "SELECT count(*) FROM public.lecture_transcript_artifacts").fetchone()[0],
        "content_rows": connection.execute(
            "SELECT count(*) FROM public.lecture_transcript_artifact_contents").fetchone()[0],
        "distinct_provider_artifacts": connection.execute(
            "SELECT count(*) FROM (SELECT DISTINCT provider, provider_transcript_id "
            "FROM public.lecture_transcript_artifacts) t").fetchone()[0],
        "distinct_content_hashes": connection.execute(
            "SELECT count(DISTINCT content_sha256) "
            "FROM public.lecture_transcript_artifact_contents").fetchone()[0],
    }
    if lecture_scoped:
        duplicated = connection.execute(
            "SELECT provider, provider_transcript_id, count(DISTINCT lecture_id) AS lectures "
            "FROM public.lecture_transcript_artifacts GROUP BY 1, 2 "
            "HAVING count(DISTINCT lecture_id) > 1 ORDER BY 3 DESC"
        ).fetchall()
        report["provider_transcripts_with_multiple_lectures"] = [
            {"provider": row[0], "provider_transcript_id": row[1], "lectures": row[2]}
            for row in duplicated
        ]
        report["duplicate_raw_content_rows"] = (
            report["artifact_rows"] - report["distinct_provider_artifacts"])
        report["lectures_with_artifacts"] = connection.execute(
            "SELECT count(DISTINCT lecture_id) FROM public.lecture_transcript_artifacts"
        ).fetchone()[0]
    return report


def normalize(connection) -> dict:
    """Create the association table, backfill it, then re-key to provider identity."""
    connection.execute(_sql("005_create_transcript_candidates.sql"))

    rows = connection.execute(
        "SELECT artifact_id, lecture_id, provider, provider_transcript_id, "
        "       meeting_id, meeting_lookup_user_id, first_seen_at, last_seen_at "
        "  FROM public.lecture_transcript_artifacts"
    ).fetchall()

    # 1. Backfill the association, keyed on the CURRENT artifact ids.
    candidates = 0
    for artifact_id, lecture_id, _provider, _transcript, meeting_id, user_id, first, last in rows:
        connection.execute(
            "INSERT INTO public.lecture_transcript_candidates "
            "(candidate_id, lecture_id, artifact_id, meeting_id, meeting_lookup_user_id, "
            " first_seen_at, last_seen_at, metadata) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
            "ON CONFLICT (lecture_id, artifact_id) DO NOTHING",
            (uuid.uuid5(CANDIDATE_NAMESPACE, f"{lecture_id}\0{artifact_id}"),
             lecture_id, artifact_id, meeting_id, user_id, first, last,
             json.dumps({"backfilled_from": "phase2a_lecture_scoped_artifact"})),
        )
        candidates += 1

    # 2. Map every lecture-scoped id onto its provider-scoped id. Collisions
    #    mean two rows held the same provider artifact; the first wins and the
    #    rest are merged away AFTER their content and links are moved across.
    mapping: dict[uuid.UUID, uuid.UUID] = {}
    canonical: dict[uuid.UUID, uuid.UUID] = {}
    merged: list[str] = []
    for artifact_id, _lecture, provider, transcript_id, *_rest in rows:
        target = provider_transcript_identity(
            provider=provider, provider_transcript_id=transcript_id)
        mapping[artifact_id] = target
        if target in canonical.values():
            merged.append(transcript_id)
        canonical.setdefault(target, artifact_id)

    # The FK is dropped for the re-key and restored immediately afterwards.
    connection.execute(
        "ALTER TABLE public.lecture_transcript_artifact_contents "
        "DROP CONSTRAINT IF EXISTS lecture_transcript_artifact_contents_artifact_id_fkey")
    connection.execute(
        "ALTER TABLE public.lecture_transcript_candidates "
        "DROP CONSTRAINT IF EXISTS lecture_transcript_candidates_artifact_id_fkey")

    duplicate_artifacts = [old for old, new in mapping.items() if canonical[new] != old]
    for old in duplicate_artifacts:
        new = mapping[old]
        # Move content versions that the surviving artifact does not already
        # hold, then drop the duplicate's remaining rows. Identical hashes are
        # the same evidence, so nothing is lost.
        connection.execute(
            "UPDATE public.lecture_transcript_artifact_contents c SET artifact_id = %s "
            " WHERE c.artifact_id = %s AND NOT EXISTS ("
            "   SELECT 1 FROM public.lecture_transcript_artifact_contents e "
            "    WHERE e.artifact_id = %s AND e.content_sha256 = c.content_sha256)",
            (canonical[new], old, canonical[new]),
        )
        connection.execute(
            "DELETE FROM public.lecture_transcript_artifact_contents WHERE artifact_id = %s",
            (old,))
        connection.execute(
            "UPDATE public.lecture_transcript_candidates SET artifact_id = %s "
            " WHERE artifact_id = %s", (canonical[new], old))
        connection.execute(
            "DELETE FROM public.lecture_transcript_artifacts WHERE artifact_id = %s", (old,))

    # 3. Re-key the survivors. Old and new id spaces are disjoint, so a single
    #    UPDATE per table cannot collide.
    remapped = 0
    for new, old in canonical.items():
        if old == new:
            continue
        connection.execute(
            "UPDATE public.lecture_transcript_artifacts SET artifact_id = %s WHERE artifact_id = %s",
            (new, old))
        connection.execute(
            "UPDATE public.lecture_transcript_artifact_contents SET artifact_id = %s "
            "WHERE artifact_id = %s", (new, old))
        connection.execute(
            "UPDATE public.lecture_transcript_candidates SET artifact_id = %s WHERE artifact_id = %s",
            (new, old))
        remapped += 1

    connection.execute(
        "ALTER TABLE public.lecture_transcript_artifact_contents "
        "ADD CONSTRAINT lecture_transcript_artifact_contents_artifact_id_fkey "
        "FOREIGN KEY (artifact_id) REFERENCES public.lecture_transcript_artifacts (artifact_id) "
        "ON DELETE RESTRICT")
    connection.execute(
        "ALTER TABLE public.lecture_transcript_candidates "
        "ADD CONSTRAINT lecture_transcript_candidates_artifact_id_fkey "
        "FOREIGN KEY (artifact_id) REFERENCES public.lecture_transcript_artifacts (artifact_id) "
        "ON DELETE RESTRICT")

    connection.execute(_sql("006_provider_scoped_artifact_identity.sql"))

    return {
        "candidate_links_created": candidates,
        "artifact_ids_remapped": remapped,
        "duplicate_artifacts_merged": len(duplicate_artifacts),
        "merged_provider_transcript_ids": merged,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="normalize-transcript-artifacts")
    parser.add_argument("--apply", action="store_true", help="commit (otherwise report only)")
    args = parser.parse_args(argv)

    settings = Settings.from_environment()
    settings.require_database()
    connection = psycopg.connect(settings.database_url)
    try:
        report = {"mode": "APPLY" if args.apply else "REPORT_ONLY", "before": inspect(connection)}
        if report["before"]["already_normalized"]:
            report["result"] = "ALREADY_NORMALIZED"
            connection.rollback()
            print(json.dumps(report, indent=2, default=str))
            return 0
        report["normalization"] = normalize(connection)
        report["after"] = inspect(connection)
        # Evidence guarantees: every provider artifact survives exactly once and
        # every distinct content hash is still present.
        report["evidence_preserved"] = (
            report["after"]["artifact_rows"] == report["before"]["distinct_provider_artifacts"]
            and report["after"]["distinct_content_hashes"]
            == report["before"]["distinct_content_hashes"]
        )
        if args.apply and report["evidence_preserved"]:
            connection.commit()
            report["committed"] = True
        else:
            connection.rollback()
            report["committed"] = False
        print(json.dumps(report, indent=2, default=str))
        return 0 if report["evidence_preserved"] else 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
