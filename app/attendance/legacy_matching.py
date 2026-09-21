"""
Verbatim port of the legacy n8n name comparator.

Source of truth: `automation/legacy_n8n/QA_One_Lecture_Safe_Exact_Recording_v8.json`,
node `Build Session and Checklist Rows` (the "Fuzzy matcher - v7" section):
`normalize`, `toTokens`, `levenshtein` and `isSimilar`.

This module is a COMPATIBILITY function, not the platform's identity
algorithm. It is intentionally not simplified, not improved and not tuned: its
only job is to reproduce what the legacy workflow would have decided, so the
new resolver can be compared against it. Everything that makes the coded
platform safer - exact-match precedence and the ambiguity guard - lives in
`resolver.py`, never in here.

Pure functions only: no database, no configuration, no logging.
"""
import re


LEGACY_MATCHER_VERSION = "legacy_is_similar_v7"

# Legacy threshold, from the export: `if (1 - dist / maxLen >= 0.8) return true`.
LEVENSHTEIN_SIMILARITY_THRESHOLD = 0.8
# Legacy `toTokens` drops tokens shorter than 2 characters ("ignore lone initials").
MIN_TOKEN_LENGTH = 2
# Legacy prefix rule requires BOTH tokens to be at least 3 characters.
MIN_PREFIX_LENGTH = 3

_NON_LETTER = re.compile(r"[^a-z]")
_WHITESPACE = re.compile(r"\s+")


def legacy_normalize(value: str) -> str:
    """
    Legacy `normalize`: lowercase, then delete every character outside a-z.

    This destroys spaces, digits, hyphens, apostrophes and accents. That is
    deliberate legacy behaviour and is reproduced exactly; the platform's own
    conservative normalizer is `normalize_speaker_label` and is unrelated.
    """
    return _NON_LETTER.sub("", str(value or "").lower())


def legacy_tokens(value: str) -> list[str]:
    """Legacy `toTokens`: lowercase, split on whitespace, strip non-letters, keep len >= 2."""
    parts = _WHITESPACE.split(str(value or "").lower())
    tokens = [_NON_LETTER.sub("", part) for part in parts if part]
    return [token for token in tokens if len(token) >= MIN_TOKEN_LENGTH]


def levenshtein(left: str, right: str) -> int:
    """Plain Levenshtein edit distance, matching the legacy matrix implementation."""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(left) + 1))
    for i, right_char in enumerate(right, start=1):
        current = [i]
        for j, left_char in enumerate(left, start=1):
            if right_char == left_char:
                current.append(previous[j - 1])
            else:
                current.append(1 + min(previous[j - 1], current[j - 1], previous[j]))
        previous = current
    return previous[len(left)]


def levenshtein_similarity(left: str, right: str) -> float:
    """Legacy `1 - dist / maxLen` over the legacy-normalized forms."""
    normalized_left = legacy_normalize(left)
    normalized_right = legacy_normalize(right)
    if not normalized_left or not normalized_right:
        return 0.0
    longest = max(len(normalized_left), len(normalized_right))
    return 1 - levenshtein(normalized_left, normalized_right) / longest


def token_subset_match(left: str, right: str) -> bool:
    """
    Legacy method 2: every token of the shorter name matches some token of the
    longer one, either exactly or as a prefix where both tokens are >= 3 chars.

    Catches "Chris" vs "Christopher" and "Ali" vs "Ali Mohamedin"; rejects
    "John Doe" vs "John Smith".
    """
    tokens_left = legacy_tokens(left)
    tokens_right = legacy_tokens(right)
    if not tokens_left or not tokens_right:
        return False
    # Legacy ties (equal token counts) take tokensA as the shorter side.
    shorter, longer = ((tokens_left, tokens_right)
                       if len(tokens_left) <= len(tokens_right)
                       else (tokens_right, tokens_left))
    return all(
        any(
            candidate == token
            or (len(token) >= MIN_PREFIX_LENGTH and len(candidate) >= MIN_PREFIX_LENGTH
                and (candidate.startswith(token) or token.startswith(candidate)))
            for candidate in longer
        )
        for token in shorter
    )


def is_similar(left: str, right: str) -> bool:
    """
    Legacy `isSimilar(a, b)` exactly: Levenshtein similarity >= 0.8, OR the
    token-subset rule.

    Note the legacy asymmetry it inherits: the argument order can matter when
    token counts are equal, so callers that need a stable answer must fix their
    own argument order. `resolver.py` always calls (speaker_label, member_name).
    """
    # Legacy guards on the normalized forms before either method runs.
    if not legacy_normalize(left) or not legacy_normalize(right):
        return False
    if levenshtein_similarity(left, right) >= LEVENSHTEIN_SIMILARITY_THRESHOLD:
        return True
    return token_subset_match(left, right)
