"""
The Positive Moment evidence policy: versions, vocabulary and thresholds.

One module owns every rule a reviewer might question, so a change to what
counts as a Positive Moment is a change to THIS file and a new
ANALYSIS_POLICY_VERSION - which makes every older analysis STALE rather than
silently reinterpreted.

THE PIPELINE
------------
    canonical cues (lecture_transcript_cues, never Graph)
        -> RECALL SELECTOR      the model proposes cue RANGES only
        -> STRUCTURAL VALIDATION deterministic: cues exist, speakers exist,
                                 trainer is never a positive speaker, quote
                                 rebuilt from the database, dedupe, trim
        -> PRECISION VERIFIER   an independent model call audits the rebuilt
                                 dialogue WITHOUT the selector's category,
                                 reason or confidence
        -> accepted moments

The model never supplies a quote, a timestamp, a speaker name that is not in
the cues, or a cue number that does not exist: everything persisted as
evidence is reconstructed by the backend from stored cues.
"""
from __future__ import annotations

import re

ANALYSIS_POLICY_VERSION = "positive_moments_v1_evidence_intelligence"
SELECTOR_PROMPT_VERSION = "positive_moments_recall_selector_v1"
VERIFIER_PROMPT_VERSION = "positive_moments_precision_verifier_v1"
LEGACY_IMPORT_VERSION = "legacy_v5_import_v1"

SOURCE_CODED_AI = "coded_ai"
SOURCE_LEGACY_V5 = "legacy_v5_import"
LEGACY_V5_COMPLETENESS = "positive_clips_v5_final"

CATEGORIES = ("trainer", "teaching_method", "content", "support", "learning_experience")

# Model spellings seen in practice, normalised to the five categories. Anything
# else is rejected (INVALID_CATEGORY), never guessed.
CATEGORY_ALIASES = {
    "trainer": "trainer", "facilitator": "trainer", "trainer_praise": "trainer",
    "teaching_method": "teaching_method", "teaching": "teaching_method",
    "method": "teaching_method", "activity": "teaching_method",
    "exercise": "teaching_method", "explanation": "teaching_method",
    "content": "content", "concept": "content", "framework": "content",
    "material": "content", "topic": "content",
    "support": "support", "coaching": "support", "help": "support",
    "learning_experience": "learning_experience", "learning": "learning_experience",
    "understanding": "learning_experience", "confidence": "learning_experience",
    "application": "learning_experience", "relevance": "learning_experience",
    "experience": "learning_experience",
}

# -- evidence length ------------------------------------------------------------
# The historical system targeted about 2-60 s. Longer legitimate exchanges are
# trimmed of unrelated leading/trailing context first; only what is still
# longer than the hard ceiling is rejected. The positive learner cues are
# never trimmed away.
TARGET_MAX_EVIDENCE_SECONDS = 60.0
HARD_MAX_EVIDENCE_SECONDS = 180.0
MIN_EVIDENCE_SECONDS = 1.0
MAX_CANDIDATE_CUES = 60

# -- dedupe ---------------------------------------------------------------------
# Two candidates whose time ranges overlap by at least this fraction of the
# shorter one are the same moment.
OVERLAP_DUPLICATE_RATIO = 0.5

# -- verifier -------------------------------------------------------------------
MIN_VERIFIER_CONFIDENCE = 0.6
MAX_SELECTOR_CANDIDATES = 40

# -- rejection codes (persisted as counts; safe to show) --------------------------
UNKNOWN_CUE = "UNKNOWN_CUE"
INVERTED_RANGE = "INVERTED_RANGE"
TOO_MANY_CUES = "TOO_MANY_CUES"
EVIDENCE_TOO_LONG = "EVIDENCE_TOO_LONG"
EVIDENCE_TOO_SHORT = "EVIDENCE_TOO_SHORT"
INVALID_CATEGORY = "INVALID_CATEGORY"
NO_POSITIVE_SPEAKER = "NO_POSITIVE_SPEAKER"
SPEAKER_NOT_IN_RANGE = "SPEAKER_NOT_IN_RANGE"
TRAINER_AS_POSITIVE_SPEAKER = "TRAINER_AS_POSITIVE_SPEAKER"
GENERIC_PRAISE_NO_TARGET = "GENERIC_PRAISE_NO_TARGET"
MEETING_TOOL_PRAISE = "MEETING_TOOL_PRAISE"
DUPLICATE_OVERLAP = "DUPLICATE_OVERLAP"
VERIFIER_REJECTED = "VERIFIER_REJECTED"
VERIFIER_LOW_CONFIDENCE = "VERIFIER_LOW_CONFIDENCE"
VERIFIER_NO_VERDICT = "VERIFIER_NO_VERDICT"

# The verifier's own rejection vocabulary (strict enum in its schema).
VERIFIER_REJECTION_CODES = (
    "GREETING_OR_SMALL_TALK", "ATTENDANCE_OR_ADMIN", "JOKE_WITHOUT_LEARNING",
    "GENERIC_PRAISE_NO_TARGET", "TRAINER_ONLY_POSITIVITY", "NEUTRAL_PARTICIPATION",
    "CORRECT_ANSWER_ONLY", "READING_OR_DEFINING", "WORKPLACE_STORY_NO_LEARNING",
    "SOFTWARE_OR_MEETING_TOOL", "REFERENT_NOT_PROVEN", "OTHER",
)
VERIFIER_TARGETS = ("trainer", "teaching", "method_or_activity", "content",
                    "support", "learning_experience", "role_application", "none")

# -- deterministic guards -----------------------------------------------------------
# A learner line made ONLY of these words has no identifiable training target.
# The verifier rejects richer cases; this guard makes the obvious ones free.
_GENERIC_WORDS = frozenset("""
thanks thank you so much very really great amazing awesome brilliant lovely nice
good all fine cool perfect wonderful fantastic excellent super ok okay yeah yes
cheers bye goodbye see everyone guys again appreciate it that was session today
lot sir miss much for and the a have day evening morning afternoon
""".split())
_MEETING_TOOL_WORDS = frozenset("""
mute unmute muted camera cam mic microphone screen share sharing connection
internet wifi teams chat button buttons link audio video volume lagging lag
""".split())
_WORD = re.compile(r"[a-z']+")


def words(text: str) -> list[str]:
    return _WORD.findall(str(text or "").casefold())


def is_generic_praise(positive_text: str) -> bool:
    """True when the positive speakers said nothing beyond generic politeness."""
    tokens = words(positive_text)
    return bool(tokens) and all(token.strip("'") in _GENERIC_WORDS for token in tokens)


_LEARNING_WORDS = frozenset("""
learn learned learning learnt understand understanding understood sense explain explained
explanation explaining framework frameworks concept concepts content course session training
trainer teach teaching taught exercise exercises activity activities role job work workplace
apply applying application practice practical useful helpful helped confident confidence
knowledge skill skills insight method methods model models approach technique tool kit
""".split())


def is_meeting_tool_praise(positive_text: str) -> bool:
    """True when the praise is about meeting controls and nothing about learning."""
    tokens = set(words(positive_text))
    return bool(tokens & _MEETING_TOOL_WORDS) and not (tokens & _LEARNING_WORDS)


def normalize_category(value) -> str | None:
    key = re.sub(r"[^a-z]+", "_", str(value or "").casefold()).strip("_")
    return CATEGORY_ALIASES.get(key)


def normalize_speaker(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def describe() -> dict:
    """Safe, persistable description of the policy in force."""
    return {
        "analysis_policy_version": ANALYSIS_POLICY_VERSION,
        "selector_prompt_version": SELECTOR_PROMPT_VERSION,
        "verifier_prompt_version": VERIFIER_PROMPT_VERSION,
        "target_max_evidence_seconds": TARGET_MAX_EVIDENCE_SECONDS,
        "hard_max_evidence_seconds": HARD_MAX_EVIDENCE_SECONDS,
        "min_verifier_confidence": MIN_VERIFIER_CONFIDENCE,
        "overlap_duplicate_ratio": OVERLAP_DUPLICATE_RATIO,
    }
