"""
The parts of the QA verdict that code owns, not the model.

Ported from the legacy export:

- delivery gate: node `Session Delivered?` — `durationMinutes < 20` must be
  FALSE to reach the AI, so exactly 20 minutes is delivered;
- non-delivered output: node `Cancelled Session Output`;
- Item 1 duration thresholds and Item 2 punctuality rules: stated as
  authoritative metadata rules in the `AI Teaching QA Combined` system message;
- `duration_score`: node `Build Session and Checklist Rows`.

The model is asked for Items 1, 2 and 7 too, and its answers are kept for
diagnostics, but the canonical values come from here and from the persisted
Phase 2C4 engagement result.
"""
from app.qa.checklist import MET, NOT_MET, PARTIALLY_MET


# Node `Session Delivered?`: leftValue `{{ $json.durationMinutes < 20 }}`
# with operation "false". Below this many minutes, no AI call is made.
DELIVERY_MINIMUM_MINUTES = 20

# System message, ITEM 1: Met >= 105, Partially Met 95..104, Not Met < 95.
# Deliberately NOT the 120-minute value used by duration_score.
ITEM1_MET_MINUTES = 105
ITEM1_PARTIAL_MINUTES = 95

# System message, ITEM 2: late start over 20 minutes, or ending more than 20
# minutes early, is the only way to fail. Early start and overrun are fine.
ITEM2_MAX_START_LATE_MINUTES = 20
ITEM2_MAX_END_EARLY_MINUTES = -20

DELIVERED = "DELIVERED"
NON_DELIVERED = "NON_DELIVERED"

# Node `Cancelled Session Output`, verbatim.
NON_DELIVERED_TRAINER = "Session not delivered"
NON_DELIVERED_EVIDENCE = "Session was cancelled or ended before delivery began."
NON_DELIVERED_JUDGEMENT = ("Session was cancelled or ended before delivery. No teaching, "
                           "objectives, or engagement were provided.")
NON_DELIVERED_TEACHING_COMMENTS = "Session cancelled or ended before teaching began."
# Legacy sets rating 1 here, not 0, while duration_score and engagement_score are 0.
NON_DELIVERED_TEACHING_RATING = 1
NON_DELIVERED_DURATION_TEXT = "0 minutes"


def delivery_status(duration_minutes) -> str:
    """Legacy `Session Delivered?`. A missing duration cannot be delivered."""
    if duration_minutes is None:
        return NON_DELIVERED
    return NON_DELIVERED if duration_minutes < DELIVERY_MINIMUM_MINUTES else DELIVERED


def item1_status(duration_minutes) -> str:
    """Item 1 from the duration alone; anything unusable is Not Met, as legacy said."""
    if not isinstance(duration_minutes, (int, float)) or isinstance(duration_minutes, bool):
        return NOT_MET
    if duration_minutes >= ITEM1_MET_MINUTES:
        return MET
    if duration_minutes >= ITEM1_PARTIAL_MINUTES:
        return PARTIALLY_MET
    return NOT_MET


def item2_status(start_difference_minutes, end_difference_minutes) -> str:
    """
    Item 2 from timing metadata alone.

    Met when the session started no more than 20 minutes late AND ended no
    more than 20 minutes early. Starting early and running over are always
    acceptable. Missing timing is Not Met rather than silently passing.
    """
    if start_difference_minutes is None or end_difference_minutes is None:
        return NOT_MET
    if start_difference_minutes > ITEM2_MAX_START_LATE_MINUTES:
        return NOT_MET
    if end_difference_minutes < ITEM2_MAX_END_EARLY_MINUTES:
        return NOT_MET
    return MET


def duration_score(duration_minutes) -> int:
    """
    Node `Build Session and Checklist Rows`:

        let duration_score = 1;
        if (durationMinutes > 120) duration_score = 5;
        else if (durationMinutes >= 115) duration_score = 4;
        else if (durationMinutes >= 110) duration_score = 3;
        else if (durationMinutes >= 105) duration_score = 2;

    Note the first band is strictly greater than 120, so 120 exactly scores 4.
    These bands are unrelated to the Item 1 thresholds.
    """
    if not isinstance(duration_minutes, (int, float)) or isinstance(duration_minutes, bool):
        return 1
    if duration_minutes > 120:
        return 5
    if duration_minutes >= 115:
        return 4
    if duration_minutes >= 110:
        return 3
    if duration_minutes >= 105:
        return 2
    return 1


def duration_text(duration_minutes) -> str:
    """Legacy `realDurationText`: "H hours" or "H hours M minutes"."""
    if not isinstance(duration_minutes, (int, float)) or isinstance(duration_minutes, bool):
        return ""
    hours, minutes = divmod(int(duration_minutes), 60)
    return f"{hours} hours" if minutes == 0 else f"{hours} hours {minutes} minutes"


def non_delivered_checklist() -> list[dict]:
    """The legacy cancelled result: 11 rows, all Not Met, same evidence string."""
    from app.qa.checklist import CHECKLIST_ITEMS, SOURCE_NON_DELIVERED
    return [
        {"checklist_order": order, "checklist_item": item, "status": NOT_MET,
         "status_source": SOURCE_NON_DELIVERED, "ai_status": None,
         "reasoning": None, "evidence_text": NON_DELIVERED_EVIDENCE, "evidence_clips": []}
        for order, item in enumerate(CHECKLIST_ITEMS, start=1)
    ]


def non_delivered_summary() -> dict:
    """Session-level values for a non-delivered session, from the legacy node."""
    return {
        "cancelled_session": True,
        "trainer_display": NON_DELIVERED_TRAINER,
        "met_count": 0, "partial_count": 0, "not_met_count": 11,
        "engagement_percentage": 0, "engagement_score": 0,
        "duration_text": NON_DELIVERED_DURATION_TEXT, "duration_score": 0,
        "teaching_quality_rating": NON_DELIVERED_TEACHING_RATING,
        "teaching_quality_comments": NON_DELIVERED_TEACHING_COMMENTS,
        "overall_judgement": NON_DELIVERED_JUDGEMENT,
    }
