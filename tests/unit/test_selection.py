from datetime import datetime, timezone

import pytest

from app.lectures.service import select_single_lecture


def row(hour):
    return {"scheduled_start": datetime(2026, 9, 4, hour, tzinfo=timezone.utc), "lecture_id": str(hour)}


def test_single_lecture_selection_safety():
    with pytest.raises(ValueError, match="no matching"):
        select_single_lecture([])
    assert select_single_lecture([row(7)])["lecture_id"] == "7"
    with pytest.raises(ValueError, match="multiple"):
        select_single_lecture([row(7), row(8)])
    assert select_single_lecture([row(7), row(8)], "10:00")["lecture_id"] == "7"
