from app.cli.compare_aptem_qa import compare_subjects


def test_comparison_reports_exact_normalized_reason_without_fuzzy_matching():
    rows = compare_subjects([" Data & AI ", "Data and AI"], ["Data &amp; AI"])
    assert rows[0]["exact_normalized_match"] is True
    assert rows[0]["reason"] == "EXACT_NORMALIZED_ACTIVE_GROUP_MATCH"
    assert rows[1]["exact_normalized_match"] is False
    assert rows[1]["reason"] == "NO_ACTIVE_APTEM_GROUP_WITH_EQUAL_NORMALIZED_TEXT"
