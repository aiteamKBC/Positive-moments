"""
The production auditor must be incapable of writing.

`tools/audit_qa_core_production.py` is pointed at a database shared with live
company systems, so "it only reads" cannot be a claim in a docstring - it has
to be a property something checks. These tests are that check.
"""
import re
from pathlib import Path

import pytest


AUDITOR = Path(__file__).resolve().parents[2] / "tools" / "audit_qa_core_production.py"

# The probe is the ONE write in the file, and it exists to be refused.
PERMITTED_WRITE_PROBE = "CREATE TEMP TABLE _qa_core_audit_write_probe"
# The auditor derives its table inventory by READING the migration files. That
# pattern names CREATE TABLE because it is matching text on disk, and it is
# never sent to the database.
MIGRATION_SCAN_PATTERN = "(?:IF NOT EXISTS )?"

MUTATION_KEYWORDS = (
    "INSERT INTO", "UPDATE ", "DELETE FROM", "TRUNCATE", "DROP ", "ALTER ",
    "CREATE INDEX", "CREATE TABLE", "GRANT ", "REVOKE ", "COPY ", "MERGE INTO",
    "REFRESH MATERIALIZED",
)


@pytest.fixture(scope="module")
def source() -> str:
    return AUDITOR.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def sql_literals(source) -> list:
    """
    Every string literal in the file that is actually SQL, docstrings excluded.

    Parsing rather than grepping is the point: the module docstring NAMES the
    keywords it forbids, and a text search cannot tell prose from a statement.
    """
    import ast

    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if text in docstrings:
                continue
            upper = text.upper()
            if "SELECT " in upper or "FROM " in upper or "TABLE" in upper:
                found.append((getattr(node, "lineno", 0), text))
    return found


def test_the_auditor_exists(source):
    assert source


def test_the_audit_does_read_real_sql(sql_literals):
    """Guard against the guard passing because it found nothing."""
    assert len(sql_literals) > 30


@pytest.mark.parametrize("keyword", MUTATION_KEYWORDS)
def test_no_mutation_sql_in_the_auditor(sql_literals, keyword):
    """
    Structural, not aspirational: no SQL string in this file may mutate.

    The single permitted exception is the write probe, which exists to be
    refused by the read-only transaction.
    """
    for lineno, text in sql_literals:
        if PERMITTED_WRITE_PROBE in text or MIGRATION_SCAN_PATTERN in text:
            continue
        assert keyword not in text.upper(), (
            f"{keyword!r} appears in SQL at line {lineno}: {text.strip()[:120]}")


def test_the_connection_is_opened_read_only(source):
    assert 'options="-c default_transaction_read_only=on"' in source


def test_the_read_only_setting_is_asserted_not_assumed(source):
    assert 'SHOW transaction_read_only' in source
    assert "ReadOnlyViolation" in source


def test_a_write_probe_must_actually_be_refused(source):
    """A guard that is never exercised is a guard nobody has checked."""
    assert PERMITTED_WRITE_PROBE in source
    assert "the write probe SUCCEEDED" in source


def test_the_auditor_calls_no_external_service(source):
    for forbidden in ("requests", "httpx", "urllib.request", "openai",
                      "anthropic", "graph.microsoft.com", "webhook"):
        assert forbidden not in source, f"{forbidden} must not be reachable"


def test_the_auditor_never_prints_a_credential(source):
    # database_url() returns the value; nothing may log or print it.
    assert not re.search(r"print\([^)]*database_url", source)
    assert not re.search(r"print\([^)]*DATABASE_URL", source)


def test_the_production_env_file_is_not_tracked():
    import subprocess

    repo = AUDITOR.parents[1]
    tracked = subprocess.run(
        ["git", "ls-files", "backend/.env"], cwd=repo, capture_output=True,
        text=True, check=False).stdout.strip()
    assert tracked == "", "backend/.env must never be committed"
