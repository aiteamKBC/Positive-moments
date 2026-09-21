"""
Speaker -> attendance-member person resolution.

This layer answers only one question: "does this transcript speaker label
correspond to a member of the frozen attendance roster?" It never answers
"is this person the trainer" - that is `roles.py`, with its own version.

Matching is conservative-first:

  1. EXACT_MATCH             raw speaker label == raw member name
  2. NORMALIZED_EXACT_MATCH  conservative normalized forms are equal
  3. LEGACY_FUZZY_MATCH      the ported legacy comparator, as a fallback only

Fuzzy matching never runs once an exact stage has produced a unique answer.

SAFETY GUARD - an intentional divergence from legacy. The legacy workflow only
ever asked a boolean question (`attendedStudents.some(isSimilar)`), so it never
had to choose between two plausible people and never recorded that it could
not. The coded platform records a person reference, so it must refuse to guess:
two or more valid candidates produce AMBIGUOUS, never a database-order pick.
"""
from app.attendance.legacy_matching import LEGACY_MATCHER_VERSION, is_similar
from app.attendance.roster import normalize_person_name


RESOLVER_VERSION = "speaker_resolver_exact_then_legacy_fuzzy_v1"

EXACT_MATCH = "EXACT_MATCH"
NORMALIZED_EXACT_MATCH = "NORMALIZED_EXACT_MATCH"
LEGACY_FUZZY_MATCH = "LEGACY_FUZZY_MATCH"
AMBIGUOUS = "AMBIGUOUS"
NO_MATCH = "NO_MATCH"
NOT_APPLICABLE = "NOT_APPLICABLE"

MATCHED_STATUSES = frozenset({EXACT_MATCH, NORMALIZED_EXACT_MATCH, LEGACY_FUZZY_MATCH})

MATCH_METHOD_BY_STATUS = {
    EXACT_MATCH: "RAW_LABEL_EQUALITY",
    NORMALIZED_EXACT_MATCH: "CONSERVATIVE_NORMALIZED_EQUALITY",
    LEGACY_FUZZY_MATCH: LEGACY_MATCHER_VERSION,
}

# Deterministic confidence markers, not probabilities. They rank the evidence
# strength of the stage that produced the answer; nothing computes with them.
MATCH_SCORE_BY_STATUS = {
    EXACT_MATCH: 1.0,
    NORMALIZED_EXACT_MATCH: 0.95,
    LEGACY_FUZZY_MATCH: 0.8,
}


def _member_sort_key(member) -> tuple:
    """Stable ordering for reporting candidates. Never used to PICK one."""
    return (member.get("display_name_normalized") or "",
            member.get("external_person_id") or "",
            member.get("email_sha256") or "")


def resolve_speaker(speaker_label_raw: str, members) -> dict:
    """
    Resolve one speaker label against the frozen roster.

    Returns the status, the matched member (when exactly one survived), the
    method, the score and the candidate count at the deciding stage.
    """
    if not members:
        # There is no roster to resolve against, which is not the same fact as
        # "this speaker is not in the roster".
        return {"resolution_status": NOT_APPLICABLE, "matched_member": None,
                "match_method": None, "match_score": None, "candidate_count": 0,
                "stage": "NO_ROSTER"}

    label = str(speaker_label_raw or "")

    exact = [member for member in members if member["display_name_raw"] == label]
    if exact:
        return _decide(EXACT_MATCH, exact, "RAW_EXACT")

    normalized_label = normalize_person_name(label)
    normalized = [member for member in members
                  if member["display_name_normalized"] == normalized_label]
    if normalized:
        return _decide(NORMALIZED_EXACT_MATCH, normalized, "NORMALIZED_EXACT")

    # Fallback only. Argument order is fixed (speaker, member) so the legacy
    # comparator's order sensitivity cannot make the result vary.
    fuzzy = [member for member in members
             if is_similar(label, member["display_name_raw"])]
    if fuzzy:
        return _decide(LEGACY_FUZZY_MATCH, fuzzy, "LEGACY_FUZZY")

    return {"resolution_status": NO_MATCH, "matched_member": None,
            "match_method": None, "match_score": None, "candidate_count": 0,
            "stage": "LEGACY_FUZZY"}


def _decide(status: str, candidates: list, stage: str) -> dict:
    ordered = sorted(candidates, key=_member_sort_key)
    if len(ordered) > 1:
        # Refuse to choose. The candidates are reported by count and by
        # sanitized reference, never resolved by arbitrary order.
        return {
            "resolution_status": AMBIGUOUS, "matched_member": None,
            "match_method": MATCH_METHOD_BY_STATUS[status], "match_score": None,
            "candidate_count": len(ordered), "stage": stage,
            "candidate_person_ids": [member.get("external_person_id") for member in ordered],
        }
    return {"resolution_status": status, "matched_member": ordered[0],
            "match_method": MATCH_METHOD_BY_STATUS[status],
            "match_score": MATCH_SCORE_BY_STATUS[status],
            "candidate_count": 1, "stage": stage}


def legacy_would_match(speaker_label_raw: str, members) -> bool:
    """
    Exactly what the legacy workflow asked: `attendedStudents.some(isSimilar)`.

    Read-only parity probe. It returns a boolean because legacy produced a
    boolean - it never identified WHICH attendee matched, which is precisely
    why it could not detect ambiguity.
    """
    label = str(speaker_label_raw or "")
    return any(is_similar(label, member["display_name_raw"]) for member in members)
