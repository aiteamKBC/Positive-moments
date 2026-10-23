"""
Remove COMMITTED test lectures - and every row that references them - from the
isolated test database.

Most integration tests roll back. A few cannot: code under test that commits by
design (Positive Moments `submit()`, the P0-B publisher worker) or tests that
need several connections. Whatever such a test commits must be removed again,
or a later test - or a later run - meets it: day-scoped QA stages then process
a stranger's lecture on the shared fixture date and claim queues hand out a
stranger's job (found in P0-A: 41 leaked lectures broke 20 unrelated tests).

`purge_lectures` deletes children before parents, discovering the foreign keys
from pg_constraint, so it follows the real schema (including proposed tables
when they are applied) instead of a hand-kept list. Transcript artifacts are
shared by provider identity, not owned by a lecture; with `with_artifacts=True`
the ones no remaining lecture can see are removed too.

FAIL CLOSED: refuses any connection that is not the approved, marked test
database (integration_db_guard.assert_isolated) before a statement is sent.
"""
from __future__ import annotations

from tools.integration_db_guard import assert_isolated

_FKS = """
SELECT format('%%I.%%I', cn.nspname, cl.relname),
       (SELECT string_agg(format('%%I', a.attname), ', ' ORDER BY k.n)
          FROM unnest(c.conkey) WITH ORDINALITY k(attnum, n)
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum),
       (SELECT string_agg(format('%%I', a.attname), ', ' ORDER BY k.n)
          FROM unnest(c.confkey) WITH ORDINALITY k(attnum, n)
          JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.attnum)
  FROM pg_constraint c
  JOIN pg_class cl ON cl.oid = c.conrelid
  JOIN pg_namespace cn ON cn.oid = cl.relnamespace
 WHERE c.contype = 'f' AND c.confrelid = %s::regclass
"""


def _purge(connection, table: str, where: str, params, depth: int = 0) -> int:
    if depth > 12:
        raise RuntimeError(f"foreign-key chain too deep under {table}")
    deleted = 0
    for child, child_cols, parent_cols in connection.execute(_FKS, (table,)).fetchall():
        if child == table:
            continue        # self-references go with the same DELETE
        deleted += _purge(connection, child,
                          f"({child_cols}) IN (SELECT {parent_cols} FROM {table} WHERE {where})",
                          params, depth + 1)
    return deleted + connection.execute(f"DELETE FROM {table} WHERE {where}", params).rowcount


def committed_lecture_ids(connection) -> set:
    return {str(row[0]) for row in connection.execute(
        "SELECT lecture_id FROM public.lecture_sessions").fetchall()}


def purge_lectures(connection, lecture_ids, *, with_artifacts: bool = False) -> int:
    """Delete these lectures and everything referencing them. Commits."""
    assert_isolated(connection)
    ids = sorted({str(i) for i in lecture_ids})
    if not ids:
        return 0
    with connection.transaction():
        artifacts = []
        if with_artifacts:
            artifacts = [str(r[0]) for r in connection.execute(
                "SELECT DISTINCT artifact_id FROM public.lecture_transcript_candidates "
                "WHERE lecture_id = ANY(%s::uuid[])", (ids,)).fetchall()]
        deleted = _purge(connection, "public.lecture_sessions", "lecture_id = ANY(%s::uuid[])", (ids,))
        if artifacts:
            deleted += _purge(connection, "public.lecture_transcript_artifacts",
                              "artifact_id = ANY(%s::uuid[]) AND NOT EXISTS (SELECT 1 FROM "
                              "public.lecture_transcript_candidates c WHERE c.artifact_id = "
                              "lecture_transcript_artifacts.artifact_id)", (artifacts,))
    return deleted
