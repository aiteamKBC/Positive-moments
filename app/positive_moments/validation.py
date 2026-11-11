"""
Stage 2: deterministic structural validation.

The selector's output is a proposal. Nothing it says is evidence until this
module has checked it against the stored cues and REBUILT the evidence:

  * every cue id exists, the range is not inverted, and not absurdly long;
  * every positive speaker actually speaks inside the range;
  * the trainer is never a positive speaker (removed; a candidate left with
    no learner is rejected);
  * the category is one of the five, after alias normalisation;
  * generic politeness and meeting-tool praise are rejected outright;
  * over-long evidence loses unrelated leading/trailing context only - the
    positive learner cues are never cut;
  * quotes, dialogue, speakers and timestamps come from the database;
  * duplicates and heavy overlaps collapse to one, deterministically.

Pure functions. Every rejection has a code, counted in the analysis row.
"""
from __future__ import annotations

from collections import Counter

from app.positive_moments import policy as p
from app.positive_moments.models import CanonicalTranscript, Candidate, Moment


class Rejected(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _speaker_set(cues) -> dict:
    """normalized -> first raw label, in order of appearance."""
    seen: dict[str, str] = {}
    for cue in cues:
        if cue.speaker and p.normalize_speaker(cue.speaker) not in seen:
            seen[p.normalize_speaker(cue.speaker)] = cue.speaker
    return seen


def _range(transcript: CanonicalTranscript, start: int, end: int) -> list:
    index = transcript.by_index
    return [index[i] for i in range(start, end + 1) if i in index]


def _trim(cues: list, positive: set) -> list:
    """
    Drop unrelated leading/trailing context while the evidence is too long.

    Leading: cues not by a positive speaker are dropped, except the single cue
    immediately before the first positive cue (the question or explanation the
    learner is responding to). Trailing: cues after the last positive cue are
    dropped. The span from the first to the last positive cue is untouchable.
    """
    def duration(items):
        return (items[-1].end_ms - items[0].start_ms) / 1000.0

    if duration(cues) <= p.TARGET_MAX_EVIDENCE_SECONDS:
        return cues
    is_positive = [p.normalize_speaker(c.speaker) in positive for c in cues]
    first = is_positive.index(True)
    last = len(cues) - 1 - is_positive[::-1].index(True)
    keep_from = max(0, first - 1)
    return cues[keep_from:last + 1]


def conversation_type(speakers: list, trainer_included: bool) -> str:
    if len(speakers) == 1:
        return "learner_statement"
    if not trainer_included:
        return "multi_learner_exchange"
    return "learner_trainer_exchange" if len(speakers) == 2 else "multi_speaker_exchange"


def build_moment(transcript: CanonicalTranscript, candidate: Candidate, *,
                 source: str = p.SOURCE_CODED_AI) -> Moment:
    """One candidate -> one validated Moment, or Rejected(code)."""
    index = transcript.by_index
    if candidate.start_cue not in index or candidate.end_cue not in index:
        raise Rejected(p.UNKNOWN_CUE)
    if candidate.end_cue < candidate.start_cue:
        raise Rejected(p.INVERTED_RANGE)
    if candidate.end_cue - candidate.start_cue + 1 > p.MAX_CANDIDATE_CUES:
        raise Rejected(p.TOO_MANY_CUES)
    category = p.normalize_category(candidate.category)
    if category is None:
        raise Rejected(p.INVALID_CATEGORY)

    cues = _range(transcript, candidate.start_cue, candidate.end_cue)
    in_range = _speaker_set(cues)
    requested = [p.normalize_speaker(s) for s in candidate.positive_speakers if str(s).strip()]
    if not requested:
        raise Rejected(p.NO_POSITIVE_SPEAKER)
    if any(name not in in_range for name in requested):
        # A named speaker who does not speak in the range: the model's claim
        # cannot be tied to the evidence.
        raise Rejected(p.SPEAKER_NOT_IN_RANGE)
    learners = [name for name in dict.fromkeys(requested)
                if not transcript.is_trainer(in_range[name])]
    if not learners:
        raise Rejected(p.TRAINER_AS_POSITIVE_SPEAKER)

    cues = _trim(cues, set(learners))
    duration = (cues[-1].end_ms - cues[0].start_ms) / 1000.0
    if duration > p.HARD_MAX_EVIDENCE_SECONDS:
        raise Rejected(p.EVIDENCE_TOO_LONG)
    if duration < p.MIN_EVIDENCE_SECONDS:
        raise Rejected(p.EVIDENCE_TOO_SHORT)

    positive_text = " ".join(c.text for c in cues
                             if p.normalize_speaker(c.speaker) in learners)
    if p.is_generic_praise(positive_text):
        raise Rejected(p.GENERIC_PRAISE_NO_TARGET)
    if p.is_meeting_tool_praise(positive_text):
        raise Rejected(p.MEETING_TOOL_PRAISE)

    speakers_here = _speaker_set(cues)
    all_speakers = list(speakers_here.values())
    positive_labels = [speakers_here[name] for name in learners if name in speakers_here]
    trainer_included = any(transcript.is_trainer(s) for s in all_speakers)
    dialogue = [{"cue_index": c.cue_index, "start_ms": c.start_ms, "end_ms": c.end_ms,
                 "speaker": c.speaker,
                 "role": ("trainer" if transcript.is_trainer(c.speaker) else "learner"),
                 "positive": p.normalize_speaker(c.speaker) in learners,
                 "text": c.text} for c in cues]
    return Moment(
        start_cue=cues[0].cue_index, end_cue=cues[-1].cue_index,
        evidence_start_ms=cues[0].start_ms, evidence_end_ms=cues[-1].end_ms,
        positive_speakers=positive_labels, all_speakers=all_speakers,
        trainer_included=trainer_included,
        conversation_type=conversation_type(all_speakers, trainer_included),
        category=category,
        exact_quote="\n".join(f"{c.speaker or 'Unknown'}: {c.text}" for c in cues),
        positive_quote=" ".join(c.text for c in cues
                                if p.normalize_speaker(c.speaker) in learners).strip(),
        dialogue=dialogue,
        reason=(candidate.reason or "")[:1000] or None,
        selector_confidence=_confidence(candidate.confidence),
        source=source)


def _confidence(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return min(max(number, 0.0), 1.0)


def _overlap(a: Moment, b: Moment) -> float:
    shared = min(a.evidence_end_ms, b.evidence_end_ms) - max(a.evidence_start_ms,
                                                             b.evidence_start_ms)
    if shared <= 0:
        return 0.0
    shorter = min(a.evidence_end_ms - a.evidence_start_ms,
                  b.evidence_end_ms - b.evidence_start_ms)
    return shared / shorter if shorter > 0 else 1.0


def deduplicate(moments: list) -> tuple[list, int]:
    """
    Heavily overlapping moments are one moment. The survivor is the more
    confident one, then the more concise one, then the earlier one - a total
    order, so the result never depends on the model's output order.
    """
    ranked = sorted(moments, key=lambda m: (-(m.selector_confidence or 0.0),
                                            m.duration_seconds, m.start_cue, m.end_cue))
    kept: list = []
    dropped = 0
    for moment in ranked:
        if any(_overlap(moment, other) >= p.OVERLAP_DUPLICATE_RATIO for other in kept):
            dropped += 1
            continue
        kept.append(moment)
    kept.sort(key=lambda m: (m.evidence_start_ms, m.start_cue))
    return kept, dropped


def validate(transcript: CanonicalTranscript, candidates, *,
             source: str = p.SOURCE_CODED_AI) -> tuple[list, Counter]:
    """All candidates -> (deduplicated moments, rejection counts)."""
    rejections: Counter = Counter()
    moments = []
    for candidate in list(candidates)[:p.MAX_SELECTOR_CANDIDATES]:
        try:
            moments.append(build_moment(transcript, candidate, source=source))
        except Rejected as exc:
            rejections[exc.code] += 1
    moments, dropped = deduplicate(moments)
    if dropped:
        rejections[p.DUPLICATE_OVERLAP] += dropped
    return moments, rejections
