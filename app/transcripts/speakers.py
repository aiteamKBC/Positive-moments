"""
Phase 2C2: canonical per-document speaker inventory.

Converts repeated raw WebVTT speaker labels on canonical cues into one stable
row per distinct label per document. It is an INVENTORY, not an identity layer.

Explicitly NOT here, and deferred to Phase 2C3 and later: trainer or learner
roles, attendance or LMS lookup, fuzzy person matching, alias maps,
cross-document person identity, speaker percentages, and engagement.
"""
import re
import unicodedata
import uuid


SPEAKER_INVENTORY_VERSION = "speaker_inventory_v1"
SPEAKER_NAMESPACE = uuid.UUID("c47f9a25-3e18-4d6b-b5a0-71e8d2f06c93")

_WHITESPACE = re.compile(r"\s+")


def normalize_speaker_label(value: str) -> str:
    """
    Conservative normalization, for FUTURE matching only.

    Does exactly four things: Unicode NFKC, trim, collapse internal whitespace,
    casefold.

    Deliberately does NOT strip accents, remove initials or honorifics, reorder
    or split names, expand nicknames, apply alias maps, or guess emails. Those
    are matching decisions, and matching is Phase 2C3's job. Two raw labels that
    normalize alike are reported as a collision and never merged.
    """
    folded = unicodedata.normalize("NFKC", str(value or ""))
    return _WHITESPACE.sub(" ", folded).strip().casefold()


def speaker_identity(*, document_id, speaker_label_raw: str,
                     inventory_version: str = SPEAKER_INVENTORY_VERSION) -> uuid.UUID:
    """
    Identity of one distinct raw label inside one document.

    The EXACT raw label is part of the key, not the normalized form, so two
    labels that merely look alike can never collapse into one row. The document
    is part of the key because the same human can carry different Teams labels
    on different days, and different humans can carry similar names - so no
    speaker is ever shared across documents at this layer.
    """
    label = str(speaker_label_raw or "")
    if not label.strip():
        raise ValueError("speaker_label_raw is required")
    return uuid.uuid5(
        SPEAKER_NAMESPACE,
        f"document:{document_id}\0version:{inventory_version.strip()}\0label:{label}",
    )


def find_normalization_collisions(raw_labels) -> list[dict]:
    """
    Distinct raw labels sharing a normalized form, within one document.

    Reported so a human can decide; never acted on automatically.
    """
    grouped: dict[str, list[str]] = {}
    for label in raw_labels:
        grouped.setdefault(normalize_speaker_label(label), []).append(label)
    return [
        {"speaker_label_normalized": normalized, "raw_label_count": len(labels)}
        for normalized, labels in sorted(grouped.items())
        if len(labels) > 1
    ]
