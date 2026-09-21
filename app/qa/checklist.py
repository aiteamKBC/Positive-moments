"""
The 11 legacy checklist items — one definition for the whole platform.

Taken verbatim from the legacy JSON TEMPLATE in the `AI Teaching QA Combined`
system message, which is also what `Cancelled Session Output` writes and what
`qa_doctors_checklist_items` stores. Output compatibility depends on these
strings being stable, so spelling ("Professional demeanor"), punctuation and
order are never adjusted for style.
"""

CHECKLIST_ITEMS = (
    "1) Session duration: Minimum of two hours",
    "2) Punctuality: Session starts and ends on time",
    "3) Professional demeanor: Maintained throughout the session",
    "4) Learning objectives: Clearly explained at the beginning",
    "5) Content alignment: Matches the curriculum/ apprenticeship standard",
    "6) Structure and pacing: Session is well-organized and appropriately timed",
    "7) Learner engagement: Evidence of interaction, questions, and activities",
    "8) Teaching methods and resources: Appropriate and inclusive",
    "9) Understanding checks: Conducted during the session",
    "10) Real-world examples: Incorporated into the content",
    "11) Next steps: Clear follow-up activities communicated",
)

CHECKLIST_ITEM_COUNT = len(CHECKLIST_ITEMS)

MET = "Met"
PARTIALLY_MET = "Partially Met"
NOT_MET = "Not Met"
CHECKLIST_STATUSES = (MET, PARTIALLY_MET, NOT_MET)

# 1-based positions of the items code controls deterministically.
DURATION_ITEM = 1
PUNCTUALITY_ITEM = 2
ENGAGEMENT_ITEM = 7

# Where a checklist status came from, recorded per row.
SOURCE_AI = "AI"
SOURCE_DETERMINISTIC_DURATION = "DETERMINISTIC_DURATION"
SOURCE_DETERMINISTIC_PUNCTUALITY = "DETERMINISTIC_PUNCTUALITY"
SOURCE_DETERMINISTIC_ENGAGEMENT = "DETERMINISTIC_ENGAGEMENT_PHASE_2C4"
SOURCE_NON_DELIVERED = "NON_DELIVERED"


def checklist_item(order: int) -> str:
    """The exact item string for a 1-based checklist order."""
    if not 1 <= order <= CHECKLIST_ITEM_COUNT:
        raise ValueError(f"checklist order out of range: {order}")
    return CHECKLIST_ITEMS[order - 1]
