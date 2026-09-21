"""
Migration-runner transaction safety.

Phase 2B found a real defect: the migration .sql files wrap themselves in
BEGIN/COMMIT, so running one verbatim inside the normalization script's
transaction executed a COMMIT mid-way and made a report-only run write.

The fix is in app/cli/normalize_transcript_artifacts._sql: strip the file's own
transaction control, and refuse the file outright if any remains, so the outer
transaction is the only thing that can commit or roll back.
"""
import io
from pathlib import Path

import pytest

from app.cli.normalize_transcript_artifacts import ROOT, _sql


MIGRATIONS = ROOT / "app" / "db" / "migrations"


def test_the_real_migrations_do_contain_their_own_transaction_control():
    """The premise of the defect: these files are standalone scripts."""
    raw = io.open(MIGRATIONS / "005_create_transcript_candidates.sql", encoding="utf-8").read()
    assert "BEGIN;" in raw
    assert "COMMIT;" in raw


@pytest.mark.parametrize("name", [
    "005_create_transcript_candidates.sql",
    "006_provider_scoped_artifact_identity.sql",
])
def test_embedded_begin_and_commit_are_stripped(name):
    body = _sql(name)
    assert "BEGIN;" not in body.upper()
    assert "COMMIT;" not in body.upper()
    # The actual DDL survives; only transaction control is removed.
    assert "ALTER TABLE" in body or "CREATE TABLE" in body


def test_loader_refuses_any_file_that_still_carries_transaction_control(tmp_path, monkeypatch):
    """
    A BEGIN that the line-anchored strip cannot see - here trailing a comment on
    the same line - must fail loudly rather than silently commit later.
    """
    migrations = tmp_path / "app" / "db" / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "999_sneaky.sql").write_text(
        "-- a comment then BEGIN; on the same line\nCREATE TABLE x (i int);\n",
        encoding="utf-8")
    monkeypatch.setattr("app.cli.normalize_transcript_artifacts.ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="transaction control"):
        _sql("999_sneaky.sql")


def test_a_rollback_statement_is_also_refused(tmp_path, monkeypatch):
    migrations = tmp_path / "app" / "db" / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "999_rollback.sql").write_text(
        "CREATE TABLE x (i int); ROLLBACK;\n", encoding="utf-8")
    monkeypatch.setattr("app.cli.normalize_transcript_artifacts.ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="transaction control"):
        _sql("999_rollback.sql")


def test_a_clean_migration_body_loads_unchanged(tmp_path, monkeypatch):
    migrations = tmp_path / "app" / "db" / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "999_clean.sql").write_text(
        "CREATE TABLE IF NOT EXISTS probe (id int);\n", encoding="utf-8")
    monkeypatch.setattr("app.cli.normalize_transcript_artifacts.ROOT", tmp_path)
    assert "CREATE TABLE IF NOT EXISTS probe" in _sql("999_clean.sql")


def test_the_word_begin_inside_ordinary_sql_text_is_not_mistaken_for_control(tmp_path, monkeypatch):
    migrations = tmp_path / "app" / "db" / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "999_words.sql").write_text(
        "COMMENT ON TABLE probe IS 'rows created since the beginning';\n", encoding="utf-8")
    monkeypatch.setattr("app.cli.normalize_transcript_artifacts.ROOT", tmp_path)
    assert "beginning" in _sql("999_words.sql")


# --- the behaviour the defect actually threatened ----------------------------


class FakeConnection:
    """Records statements and whether commit/rollback was reached."""

    def __init__(self):
        self.statements = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def execute(self, sql, params=None):
        text = str(sql)
        self.statements.append(text)
        # A real driver would end the transaction here; assert we never send one.
        assert "COMMIT;" not in text.upper(), "a migration body leaked a COMMIT"
        assert "ROLLBACK;" not in text.upper(), "a migration body leaked a ROLLBACK"

        class Result:
            @staticmethod
            def fetchone():
                return (0,)

            @staticmethod
            def fetchall():
                return []
        return Result()

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_dry_run_rolls_back_and_never_commits(monkeypatch):
    """Report-only mode must reach rollback(), never commit()."""
    import app.cli.normalize_transcript_artifacts as module

    connection = FakeConnection()
    monkeypatch.setattr(module.psycopg, "connect", lambda url: connection)
    monkeypatch.setattr(module, "inspect", lambda conn: {
        "already_normalized": False, "artifact_rows": 3, "content_rows": 3,
        "distinct_provider_artifacts": 3, "distinct_content_hashes": 3,
        "provider_transcripts_with_multiple_lectures": [],
        "duplicate_raw_content_rows": 0, "lectures_with_artifacts": 1,
    })
    monkeypatch.setattr(module, "normalize", lambda conn: {
        "candidate_links_created": 3, "artifact_ids_remapped": 3,
        "duplicate_artifacts_merged": 0, "merged_provider_transcript_ids": [],
    })
    monkeypatch.setattr(module.Settings, "from_environment",
                        classmethod(lambda cls: _settings()))

    assert module.main([]) == 0
    assert connection.rolled_back is True
    assert connection.committed is False


def test_apply_commits_when_explicitly_requested(monkeypatch):
    import app.cli.normalize_transcript_artifacts as module

    connection = FakeConnection()
    states = iter([
        {"already_normalized": False, "artifact_rows": 3, "content_rows": 3,
         "distinct_provider_artifacts": 3, "distinct_content_hashes": 3,
         "provider_transcripts_with_multiple_lectures": [],
         "duplicate_raw_content_rows": 0, "lectures_with_artifacts": 1},
        {"already_normalized": True, "artifact_rows": 3, "content_rows": 3,
         "distinct_provider_artifacts": 3, "distinct_content_hashes": 3},
    ])
    monkeypatch.setattr(module.psycopg, "connect", lambda url: connection)
    monkeypatch.setattr(module, "inspect", lambda conn: next(states))
    monkeypatch.setattr(module, "normalize", lambda conn: {
        "candidate_links_created": 3, "artifact_ids_remapped": 3,
        "duplicate_artifacts_merged": 0, "merged_provider_transcript_ids": [],
    })
    monkeypatch.setattr(module.Settings, "from_environment",
                        classmethod(lambda cls: _settings()))

    assert module.main(["--apply"]) == 0
    assert connection.committed is True


def test_apply_refuses_to_commit_when_evidence_would_be_lost(monkeypatch):
    """The evidence guard, not just the --apply flag, decides."""
    import app.cli.normalize_transcript_artifacts as module

    connection = FakeConnection()
    states = iter([
        {"already_normalized": False, "artifact_rows": 3, "content_rows": 3,
         "distinct_provider_artifacts": 3, "distinct_content_hashes": 3,
         "provider_transcripts_with_multiple_lectures": [],
         "duplicate_raw_content_rows": 0, "lectures_with_artifacts": 1},
        # A hash vanished: normalization lost evidence.
        {"already_normalized": True, "artifact_rows": 3, "content_rows": 2,
         "distinct_provider_artifacts": 3, "distinct_content_hashes": 2},
    ])
    monkeypatch.setattr(module.psycopg, "connect", lambda url: connection)
    monkeypatch.setattr(module, "inspect", lambda conn: next(states))
    monkeypatch.setattr(module, "normalize", lambda conn: {
        "candidate_links_created": 3, "artifact_ids_remapped": 3,
        "duplicate_artifacts_merged": 0, "merged_provider_transcript_ids": [],
    })
    monkeypatch.setattr(module.Settings, "from_environment",
                        classmethod(lambda cls: _settings()))

    assert module.main(["--apply"]) == 1
    assert connection.committed is False
    assert connection.rolled_back is True


def _settings():
    class Stub:
        database_url = "postgresql://stub/stub"

        def require_database(self):
            return None
    return Stub()
