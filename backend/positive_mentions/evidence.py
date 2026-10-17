"""
What one stored positive clip says about itself, read for display.

The n8n v5 workflow writes each candidate into `qa_doctors_sessions.positive_clips`
with two independent classifications:

  * the EVIDENCE type - POSITIVE_MOMENT or OFSTED_CASE_STUDY (or REJECT, when
    the verifier refused the Selector's candidate as evidence), and
  * the SOCIAL MEDIA formats it could support - quote_post, reel, carousel.

Neither implies the other: an OFSTED case study can be a reel without being a
positive moment. Nothing here reclassifies anything. Every value is read from
the clip as stored; a field the clip does not carry stays None, never False,
because "the workflow did not say" is not "the workflow said no". Nothing here
writes to the clip either: human review lives in its own table.

Where the stored shape and the field names in use differ, both are read. The
verifier writes its verdict inside `semantic_verification` and its social
verdict inside `social_media_verification`; a flat top-level field of the same
meaning, if a later workflow version writes one, wins.
"""
import re

POSITIVE_MOMENT = "POSITIVE_MOMENT"
OFSTED_CASE_STUDY = "OFSTED_CASE_STUDY"
REJECT = "REJECT"
EVIDENCE_TYPES = (POSITIVE_MOMENT, OFSTED_CASE_STUDY, REJECT)

# Where the final type was read from, most authoritative first.
SOURCE_VERIFIED = "verified"
SOURCE_STORED = "evidence_type"
SOURCE_REASON_PREFIX = "reason_prefix"

ACCEPTED = "accepted"
REJECTED = "rejected"
NOT_VERIFIED = "not_verified"
UNRECOGNISED = "unrecognised"
_ACCEPT_VERDICTS = {"accept", "accepted"}
_REJECT_VERDICTS = {"reject", "rejected"}

QUOTE_POST = "quote_post"
REEL = "reel"
CAROUSEL = "carousel"
SOCIAL_FORMATS = (QUOTE_POST, REEL, CAROUSEL)

# Legacy clips carry the type only as the first word of `reason`, e.g.
# "OFSTED_CASE_STUDY: the tutor walks through ...". A compatibility fallback,
# used only when no stored type exists.
_REASON_PREFIX = re.compile(r"^\s*(POSITIVE_MOMENT|OFSTED_CASE_STUDY)\s*:")


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _flag(value):
    return value if isinstance(value, bool) else None


def _type(value) -> str | None:
    text = _text(value)
    return text.upper() if text and text.upper() in EVIDENCE_TYPES else None


def _first(*values):
    return next((value for value in values if value is not None), None)


def evidence_classification(clip: dict) -> dict:
    """The final evidence type and where it came from; the Selector's for contrast."""
    semantic = _dict(clip.get("semantic_verification"))
    social = _dict(clip.get("social_media_verification"))
    verified = _first(_type(semantic.get("final_evidence_type")),
                      _type(clip.get("verified_evidence_type")),
                      _type(semantic.get("verified_evidence_type")),
                      _type(social.get("final_evidence_type")))
    stored = _type(clip.get("evidence_type"))
    match = _REASON_PREFIX.match(clip.get("reason") or "") if isinstance(clip.get("reason"), str) else None
    if verified:
        final, source = verified, SOURCE_VERIFIED
    elif stored:
        final, source = stored, SOURCE_STORED
    elif match:
        final, source = match.group(1), SOURCE_REASON_PREFIX
    else:
        final, source = None, None
    selector = _first(_type(clip.get("selector_evidence_type")),
                      _type(semantic.get("selector_evidence_type")))
    reclassified = _first(_flag(clip.get("evidence_type_reclassified")),
                          _flag(semantic.get("evidence_type_reclassified")))
    return {
        "evidence_type": stored,
        "final_evidence_type": final,
        "evidence_type_source": source,
        "selector_evidence_type": selector,
        "evidence_type_reclassified": reclassified,
    }


def semantic_fields(clip: dict) -> dict:
    """The semantic verifier's advisory verdict. Absent means not verified, never accepted."""
    nested = _dict(clip.get("semantic_verification"))
    verdict = _first(_text(clip.get("semantic_verdict")), _text(nested.get("verdict")))
    lowered = (verdict or "").lower()
    if verdict is None:
        status = NOT_VERIFIED
    elif lowered in _ACCEPT_VERDICTS:
        status = ACCEPTED
    elif lowered in _REJECT_VERDICTS:
        status = REJECTED
    else:
        status = UNRECOGNISED
    return {
        "semantic_verdict": verdict,
        "semantic_status": status,
        "semantic_confidence": _first(_number(clip.get("semantic_confidence")),
                                      _number(nested.get("confidence"))),
        "semantic_reason": _first(_text(clip.get("semantic_reason")), _text(nested.get("reason"))),
        "semantic_advisory_pass": _first(_flag(clip.get("semantic_advisory_pass")),
                                         _flag(nested.get("advisory_pass"))),
    }


def _formats(value) -> list[str] | None:
    if not isinstance(value, list):
        return None
    seen = []
    for item in value:
        name = _text(item)
        if name and name.lower() not in seen:
            seen.append(name.lower())
    return seen


def _cue_times(clip: dict) -> tuple[dict, dict]:
    """Cue number -> the stored transcript timestamp, from the clip's own dialogue."""
    starts, ends = {}, {}
    for line in clip.get("dialogue") or []:
        if not isinstance(line, dict):
            continue
        if _number(line.get("start_cue")) is not None and _text(line.get("start")):
            starts.setdefault(line["start_cue"], line["start"])
        if _number(line.get("end_cue")) is not None and _text(line.get("end")):
            ends.setdefault(line["end_cue"], line["end"])
    return starts, ends


def _reel_segments(clip: dict) -> list[dict] | None:
    value = clip.get("reel_segments")
    if not isinstance(value, list):
        return None
    starts, ends = _cue_times(clip)
    segments = []
    for item in value:
        if not isinstance(item, dict):
            continue
        start_cue, end_cue = _number(item.get("start_cue")), _number(item.get("end_cue"))
        segments.append({
            "start_cue": start_cue, "end_cue": end_cue,
            # Read from the stored dialogue only; None when no line carries the cue.
            "start": starts.get(start_cue), "end": ends.get(end_cue),
        })
    return segments


def social_fields(clip: dict) -> dict:
    """What the workflow proposed for social media. Never a publishing approval."""
    verification = _dict(clip.get("social_media_verification"))
    stored = _formats(clip.get("social_media_formats"))
    primary = _text(clip.get("social_media_primary_format"))
    candidates = [name for name in (stored or []) if name in SOCIAL_FORMATS]
    if primary and primary.lower() in SOCIAL_FORMATS and primary.lower() not in candidates:
        candidates.insert(0, primary.lower())
    outline = clip.get("carousel_slide_outline")
    return {
        "social_media_primary_format": primary.lower() if primary else None,
        "social_media_formats": stored,
        "social_formats": candidates,
        "is_social_candidate": bool(candidates),
        "social_media_ready": _flag(clip.get("social_media_ready")),
        "social_media_rationale": _text(clip.get("social_media_rationale")),
        "social_verification_status": _first(_text(clip.get("social_verification_status")),
                                             _text(verification.get("status"))),
        "social_verifier_confidence": _first(_number(clip.get("social_verifier_confidence")),
                                             _number(verification.get("confidence"))),
        "social_verifier_reason": _first(_text(clip.get("social_verifier_reason")),
                                         _text(verification.get("reason"))),
        "reel_edit_mode": _text(clip.get("reel_edit_mode")),
        "reel_segments": _reel_segments(clip),
        "reel_estimated_duration_seconds": _number(clip.get("reel_estimated_duration_seconds")),
        # Strings, in the order the workflow wrote them. Never re-sorted.
        "carousel_slide_outline": ([item for item in outline if isinstance(item, str)]
                                   if isinstance(outline, list) else None),
    }


def describe(clip: dict) -> dict:
    """Every display field derived from one stored clip."""
    return {
        **evidence_classification(clip),
        **semantic_fields(clip),
        **social_fields(clip),
        "feedback_scope": _text(clip.get("feedback_scope")),
        "conversation_type": _text(clip.get("conversation_type")),
        "trainer_included": _flag(clip.get("trainer_included")),
        "selector_reason": _text(clip.get("selector_reason")),
    }


def summarise(clips: list[dict]) -> dict:
    """
    Lecture totals from the clips' own metadata. A clip counts once per bucket:
    one that supports both a reel and a carousel is one social candidate.
    """
    def count(predicate):
        return sum(1 for clip in clips if predicate(clip))

    return {
        "total": len(clips),
        "ai_accepted": count(lambda clip: clip["semantic_status"] == ACCEPTED),
        "ai_rejected": count(lambda clip: clip["semantic_status"] == REJECTED),
        "not_verified": count(lambda clip: clip["semantic_status"] == NOT_VERIFIED),
        "positive_moments": count(lambda clip: clip["final_evidence_type"] == POSITIVE_MOMENT),
        "ofsted_case_studies": count(lambda clip: clip["final_evidence_type"] == OFSTED_CASE_STUDY),
        "rejected_as_evidence": count(lambda clip: clip["final_evidence_type"] == REJECT),
        "unclassified": count(lambda clip: clip["final_evidence_type"] is None),
        "social_candidates": count(lambda clip: clip["is_social_candidate"]),
        "reel": count(lambda clip: REEL in clip["social_formats"]),
        "carousel": count(lambda clip: CAROUSEL in clip["social_formats"]),
        "quote_post": count(lambda clip: QUOTE_POST in clip["social_formats"]),
    }
