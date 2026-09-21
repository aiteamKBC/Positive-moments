from app.common.hashing import lecture_identity


def identity(event_id, ical):
    return lecture_identity(calendar_user_upn="Owner@Example.com", calendar_event_id=event_id, i_cal_uid=ical)


def test_rerun_same_occurrence_has_same_identity():
    assert identity("event-1", "ical-1") == identity("changed-graph-id", "ical-1")


def test_same_subject_twice_one_day_is_not_collapsed():
    assert identity("event-1", "ical-1") != identity("event-2", "ical-2")


def test_recurring_occurrences_are_distinct_and_each_stable():
    first = identity("occurrence-1", "series-occurrence-1")
    second = identity("occurrence-2", "series-occurrence-2")
    assert first != second
    assert first == identity("new-event-id", "series-occurrence-1")

