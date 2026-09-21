from app.lectures.matching import match_active_group, normalize_group


def test_active_group_normalization_preserves_business_rule():
    assert normalize_group("  Data &amp;   AI  ") == "data & ai"


def test_group_matching_is_exact_after_normalization_not_fuzzy():
    assert match_active_group("DATA & AI", ["Data &amp; AI"]) == "Data &amp; AI"
    assert match_active_group("Data and AI", ["Data &amp; AI"]) is None

