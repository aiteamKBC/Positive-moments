"""
The LMS module-title query against a real PostgreSQL.

A throwaway `curriculum.modules` is created in the isolated test database for
the duration of the test (the repository opens its own read-only connection,
so the fixture rows have to be committed) and dropped afterwards.
"""
import psycopg
import pytest

from app.common.errors import LMS_MODULE_QUERY_ERROR, PlatformError
from app.config.settings import Settings
from app.db.repositories.lms import LmsModuleRepository


def _url():
    # Inside the suite this is the approved isolated test database.
    url = Settings.from_environment().database_url
    if not url:
        pytest.skip("no approved test database (TEST_DATABASE_URL)")
    return url


@pytest.fixture
def lms_modules():
    connection = psycopg.connect(_url(), autocommit=True)
    existed = connection.execute(
        "SELECT 1 FROM pg_namespace WHERE nspname = 'curriculum'").fetchone()
    if existed and connection.execute("SELECT to_regclass('curriculum.modules')").fetchone()[0]:
        connection.close()
        pytest.skip("the test database already holds a curriculum.modules table")
    connection.execute("CREATE SCHEMA IF NOT EXISTS curriculum")
    connection.execute("CREATE TABLE curriculum.modules (module_catalogue_id text, title text,"
                       " deleted_at timestamptz, is_programme_deleted boolean)")
    try:
        yield connection
    finally:
        connection.execute("DROP TABLE curriculum.modules")
        if not existed:
            connection.execute("DROP SCHEMA curriculum")
        connection.close()


def test_titles_are_distinct_trimmed_and_never_null_or_blank(lms_modules):
    lms_modules.execute(
        "INSERT INTO curriculum.modules (module_catalogue_id, title) VALUES "
        "('1', 'Strategy &amp; Planning'), ('2', '  Strategy &amp; Planning  '), "
        "('3', NULL), ('4', ''), ('5', '   '), ('6', 'AI in Project Control'), "
        "('7', 'AI in Project Control')")
    titles = LmsModuleRepository(_url()).load_module_titles()
    assert titles == ["AI in Project Control", "Strategy &amp; Planning"]


def test_deleted_modules_and_modules_of_deleted_programmes_are_excluded(lms_modules):
    lms_modules.execute(
        "INSERT INTO curriculum.modules (title, deleted_at, is_programme_deleted) VALUES "
        "('Live Module', NULL, false), ('Live Unknown Programme', NULL, NULL), "
        "('Deleted Module', now(), false), ('Deleted Programme', NULL, true), "
        "('Live Module', now(), false)")
    titles = LmsModuleRepository(_url()).load_module_titles()
    assert titles == ["Live Module", "Live Unknown Programme"]


def test_the_query_runs_in_a_read_only_transaction(lms_modules, monkeypatch):
    import app.db.repositories.lms as lms

    monkeypatch.setattr(lms, "MODULE_TITLES_SQL",
                        "INSERT INTO curriculum.modules (title) VALUES ('x') RETURNING title")
    with pytest.raises(PlatformError) as refused:
        LmsModuleRepository(_url()).load_module_titles()
    assert refused.value.code == LMS_MODULE_QUERY_ERROR
    assert lms_modules.execute("SELECT count(*) FROM curriculum.modules").fetchone()[0] == 0
