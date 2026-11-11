"""
Reuse historically analysed Positive Clips V5 results - only when provable.

`qa_doctors_sessions.positive_clips` holds V5 moments for many lectures. Paying
for a second analysis of the same transcript would be waste; trusting a V5
result that was computed from a DIFFERENT transcript would be worse. So a V5
result is imported only when every one of its clips is proven against the
CURRENT canonical cues:

  * the lecture's legacy row is V5-final (`positive_clips_v5_final`);
  * each clip's timestamps land on canonical cues, and its quote is found in
    the text of those cues (word-level match >= LEGACY_MATCH_RATIO);
  * each clip then passes the same structural validation a coded candidate
    does (speakers present, trainer never positive, category valid, not
    generic praise) - rebuilt from the database, not copied from JSON.

If ANY clip cannot be proven, the legacy result is STALE as a whole and the
lecture is re-analysed from canonical cues. A partial import would present a
mixture of two transcripts as one analysis.

An empty V5 result proves nothing about the current transcript (there is no
evidence to check), so it is never imported as NO_POSITIVE_MOMENTS.

Deterministic: the same legacy JSON and the same cues always import the same
moments, and `legacy_source_fingerprint` makes a re-import a no-op.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field

from app.positive_moments import policy as p
from app.positive_moments.models import CanonicalTranscript, Candidate
from app.positive_moments.validation import Rejected, build_moment

LEGACY_MATCH_RATIO = 0.9
WINDOW_MARGIN_MS = 1500

IMPORTED = "IMPORTED"
STALE = "STALE"
NOT_AVAILABLE = "NOT_AVAILABLE"

_TIMESTAMP = re.compile(r"^(\d+):([0-5]\d):([0-5]\d(?:\.\d+)?)$")


@dataclass
class LegacyImport:
    status: str
    reason: str
    legacy_fingerprint: str | None = None
    moments: list = field(default_factory=list)
    clip_count: int = 0


def _ms(value) -> int | None:
    match = _TIMESTAMP.match(str(value or "").strip())
    if not match:
        return None
    return int(round((int(match.group(1)) * 3600 + int(match.group(2)) * 60
                      + float(match.group(3))) * 1000))


def legacy_clips(positive_clips) -> list:
    value = positive_clips
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if isinstance(value, dict):
        value = value.get("clips", value.get("positive_clips", []))
    return [clip for clip in value if isinstance(clip, dict)] if isinstance(value, list) else []


def fingerprint(positive_clips) -> str:
    canonical = json.dumps(legacy_clips(positive_clips), sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _match_ratio(needle: list, haystack: list) -> float:
    if not needle:
        return 0.0
    matcher = difflib.SequenceMatcher(a=needle, b=haystack, autojunk=False)
    return sum(block.size for block in matcher.get_matching_blocks()) / len(needle)


def import_clip(transcript: CanonicalTranscript, clip: dict):
    start_ms, end_ms = _ms(clip.get("start")), _ms(clip.get("end"))
    if start_ms is None or end_ms is None or end_ms <= start_ms:
        raise Rejected("LEGACY_TIMESTAMP_INVALID")
    window = [c for c in transcript.cues
              if c.end_ms >= start_ms - WINDOW_MARGIN_MS and c.start_ms <= end_ms + WINDOW_MARGIN_MS]
    if not window:
        raise Rejected("LEGACY_TIMESTAMP_OUTSIDE_TRANSCRIPT")
    quote = clip.get("positive_quote") or clip.get("quote") or ""
    needle = p.words(quote)
    haystack = p.words(" ".join(c.text for c in window))
    if _match_ratio(needle, haystack) < LEGACY_MATCH_RATIO:
        raise Rejected("LEGACY_QUOTE_NOT_IN_CANONICAL_CUES")
    tight = [c for c in window if c.end_ms > start_ms and c.start_ms < end_ms] or window
    speakers = clip.get("positive_speakers")
    if not isinstance(speakers, list) or not speakers:
        speakers = [clip.get("speaker")] if clip.get("speaker") else []
    semantic = clip.get("semantic_verification") if isinstance(
        clip.get("semantic_verification"), dict) else {}
    moment = build_moment(transcript, Candidate(
        start_cue=tight[0].cue_index, end_cue=tight[-1].cue_index,
        positive_speakers=tuple(str(s) for s in speakers),
        category=clip.get("category"), reason=clip.get("reason"),
        confidence=clip.get("confidence")), source=p.SOURCE_LEGACY_V5)
    moment.verifier_verdict = "LEGACY_V5_VERIFIED"
    try:
        moment.verifier_confidence = min(max(float(semantic.get("confidence")), 0.0), 1.0)
    except (TypeError, ValueError):
        moment.verifier_confidence = None
    moment.verifier_detail = {"legacy_import_version": p.LEGACY_IMPORT_VERSION,
                              "legacy_start_cue": clip.get("start_cue"),
                              "legacy_end_cue": clip.get("end_cue")}
    return moment


def try_import(transcript: CanonicalTranscript, *, completeness, positive_clips) -> LegacyImport:
    if completeness != p.LEGACY_V5_COMPLETENESS:
        return LegacyImport(NOT_AVAILABLE, "NO_V5_FINAL_RESULT")
    clips = legacy_clips(positive_clips)
    legacy_fp = fingerprint(positive_clips)
    if not clips:
        return LegacyImport(STALE, "V5_RESULT_EMPTY_UNPROVABLE", legacy_fp)
    moments = []
    for clip in clips:
        try:
            moments.append(import_clip(transcript, clip))
        except Rejected as exc:
            return LegacyImport(STALE, exc.code, legacy_fp, clip_count=len(clips))
    ranges = {(m.start_cue, m.end_cue) for m in moments}
    if len(ranges) != len(moments):
        # Two V5 clips resolving to the same cue range: not a clean lineage.
        return LegacyImport(STALE, "LEGACY_CLIPS_COLLIDE_ON_CANONICAL_CUES", legacy_fp,
                            clip_count=len(clips))
    moments.sort(key=lambda m: (m.evidence_start_ms, m.start_cue))
    return LegacyImport(IMPORTED, "ALL_CLIPS_PROVEN_AGAINST_CANONICAL_CUES", legacy_fp,
                        moments=moments, clip_count=len(clips))
