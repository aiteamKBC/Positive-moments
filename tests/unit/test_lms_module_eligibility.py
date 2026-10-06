"""
A Teams lecture is eligible when its subject matches an active Aptem group
OR an LMS module title (curriculum.modules). Same normalization, same exact
match, one lecture however many sources know it.
"""
from datetime import date

from app.common.errors import LMS_MODULE_QUERY_ERROR, PlatformError
from app.db.repositories.lms import MODULE_TITLES_SQL, LmsModuleRepository
from app.lectures.matching import index_module_names, match_module

from tests.unit.test_canonical_registry import Groups, ManyEvents, _event
from tests.unit.test_service import Registry, Runs, service

DAY = date(2026, 9, 4)


class Lms:
    def __init__(self, titles=None, error=None):
        self.titles, self.error, self.calls = titles or [], error, 0

    def load_module_titles(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.titles


def discover(events, *, aptem, lms=None, registry=None):
    registry = registry or Registry()
    svc = service(registry, Runs(), calendar=ManyEvents(events), aptem=Groups(aptem))
    svc.lms_repository = lms
    return svc.discover_day(object(), object(), DAY), registry


# --- the rule -------------------------------------------------------------------

def match(subject, aptem=(), lms=()):
    return match_module(subject, index_module_names(list(aptem)), index_module_names(list(lms)))


def test_1_aptem_only_is_eligible():
    result = match("Ray-PMO", aptem=["Ray-PMO"])
    assert (result.eligible, result.matched_source, result.module) == (True, "aptem", "Ray-PMO")


def test_2_lms_only_is_eligible():
    result = match("Femi Strategy & Planning - Jan 26", lms=["Femi Strategy &amp; Planning - Jan 26"])
    assert (result.eligible, result.matched_source) == (True, "lms")
    assert result.module == "Femi Strategy & Planning - Jan 26"      # readable, not &amp;


def test_3_both_is_eligible_and_keeps_the_aptem_module():
    result = match("Data & AI", aptem=["Data &amp; AI"], lms=["data & ai"])
    assert (result.eligible, result.matched_source, result.module) == (True, "both", "Data &amp; AI")


def test_4_neither_is_not_eligible():
    result = match("Weekly Meeting", aptem=["Ray-PMO"], lms=["Data & AI"])
    assert (result.eligible, result.matched_source, result.module) == (False, "none", None)


def test_5_html_entities_in_lms_titles_match_the_readable_teams_subject():
    assert match("Strategy & Planning", lms=["Strategy &amp; Planning"]).matched_in_lms
    assert match("G1-Keith-Strategy&Planning", lms=["G1-Keith-Strategy&amp;Planning"]).matched_in_lms


def test_5b_matching_stays_exact_not_fuzzy():
    # Separator spacing is NOT reinterpreted - the existing rule, unchanged.
    assert not match("G2-Keith-Strategy & Planning", lms=["G2-Keith- Strategy&amp; Planning"]).eligible
    assert not match("Strategy and Planning", lms=["Strategy &amp; Planning"]).eligible


def test_6_null_and_blank_titles_are_ignored():
    assert index_module_names([None, "", "   ", " \t ", "Real Module"]) == {
        "real module": "Real Module"}
    assert not match("", lms=["", "  "]).eligible


def test_6b_the_lms_query_itself_drops_null_blank_and_duplicate_titles():
    sql = " ".join(MODULE_TITLES_SQL.split())
    assert "SELECT DISTINCT BTRIM(title)" in sql
    assert "title IS NOT NULL" in sql and "BTRIM(title) <> ''" in sql
    assert "FROM curriculum.modules" in sql
    assert "deleted_at IS NULL" in sql and "NOT COALESCE(is_programme_deleted, false)" in sql


# --- through discovery ------------------------------------------------------------

def test_discovery_admits_an_lms_only_lecture_and_reports_its_source():
    summary, registry = discover(
        [_event("a", "Ray-PMO"), _event("b", "Femi Strategy & Planning - Jan 26", hour=11),
         _event("c", "Weekly Meeting", hour=15)],
        aptem=["Ray-PMO"], lms=Lms(["Femi Strategy &amp; Planning - Jan 26"]))

    assert sorted(row.subject for row in registry.rows.values()) == [
        "Femi Strategy & Planning - Jan 26", "Ray-PMO"]
    assert summary["canonical_lecture_candidates"] == 2
    assert summary["module_match_sources"] == {"aptem": 1, "lms": 1, "both": 0}
    assert summary["lms_only_matches"] == [{"subject": "Femi Strategy & Planning - Jan 26",
                                            "lms_module": "Femi Strategy & Planning - Jan 26"}]
    assert summary["lms_module_source"] == {"status": "LOADED", "titles_loaded": 1}
    (excluded,) = summary["non_canonical_calendar_event_details"]
    assert (excluded["subject"], excluded["matched_source"]) == ("Weekly Meeting", "none")


def test_3b_a_lecture_in_both_sources_is_one_lecture_with_its_aptem_module():
    summary, registry = discover([_event("a", "Data & AI")],
                                 aptem=["Data & AI"], lms=Lms(["Data &amp; AI", "DATA & AI"]))
    (row,) = registry.rows.values()
    assert row.module == "Data & AI"
    assert summary["canonical_lecture_candidates"] == 1
    assert summary["module_match_sources"] == {"aptem": 0, "lms": 0, "both": 1}


def test_7_duplicate_lms_titles_never_duplicate_a_lecture():
    summary, registry = discover([_event("a", "Risk Management")], aptem=["Other"],
                                 lms=Lms(["Risk Management", "Risk Management", " risk  management "]))
    assert len(registry.rows) == 1
    assert summary["canonical_lecture_candidates"] == 1


def test_8_without_an_lms_source_discovery_is_aptem_only_as_before():
    summary, registry = discover([_event("a", "Ray-PMO"), _event("b", "Femi", hour=11)],
                                 aptem=["Ray-PMO"], lms=None)
    assert [row.subject for row in registry.rows.values()] == ["Ray-PMO"]
    assert summary["lms_module_source"] == {"status": "NOT_CONFIGURED", "titles_loaded": 0}
    assert summary["non_canonical_calendar_event_details"][0]["exclusion_reason"] == "NOT_AN_ACTIVE_APTEM_GROUP"


def test_9_an_lms_failure_is_reported_as_a_failure_and_aptem_still_matches():
    failing = Lms(error=PlatformError(LMS_MODULE_QUERY_ERROR, "LMS module query failed (OperationalError)"))
    summary, registry = discover([_event("a", "Ray-PMO"), _event("b", "Femi", hour=11)],
                                 aptem=["Ray-PMO"], lms=failing)

    assert [row.subject for row in registry.rows.values()] == ["Ray-PMO"]
    assert summary["status"] == "COMPLETED"
    assert summary["lms_module_source"] == {"status": "FAILED", "titles_loaded": 0,
                                            "error_code": LMS_MODULE_QUERY_ERROR}
    assert summary["metadata"]["lms_module_source"]["status"] == "FAILED"
    (excluded,) = summary["non_canonical_calendar_event_details"]
    assert excluded["lms_source_status"] == "FAILED"


def test_9b_an_empty_lms_is_loaded_not_failed():
    summary, _ = discover([_event("a", "Ray-PMO")], aptem=["Ray-PMO"], lms=Lms([]))
    assert summary["lms_module_source"] == {"status": "LOADED", "titles_loaded": 0}


def test_9c_a_connection_failure_never_carries_the_connection_string():
    repository = LmsModuleRepository("postgresql://lms_user:s3cret-value@127.0.0.1:1/lms?connect_timeout=1")
    try:
        repository.load_module_titles()
    except PlatformError as exc:
        assert exc.code == LMS_MODULE_QUERY_ERROR
        assert "s3cret-value" not in str(exc) and exc.__cause__ is None
    else:
        raise AssertionError("an unreachable LMS must raise")


def test_the_lms_is_read_once_per_day_not_once_per_event():
    lms = Lms(["Module A"])
    discover([_event(str(n), f"Event {n}", hour=8 + n % 10) for n in range(12)], aptem=["Module A"], lms=lms)
    assert lms.calls == 1


def test_aptem_still_stops_discovery_when_it_returns_no_groups():
    """The existing fail-closed guard on a wrong/empty Aptem source is kept."""
    try:
        discover([_event("a", "Femi")], aptem=[], lms=Lms(["Femi"]))
    except PlatformError as exc:
        assert exc.code == "no_active_aptem_groups"
    else:
        raise AssertionError("an empty Aptem source must still stop discovery")
