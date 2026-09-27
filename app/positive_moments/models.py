"""Value types for Positive Moment evidence. Pure data; no I/O."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from app.positive_moments.policy import normalize_speaker


@dataclass(frozen=True)
class Cue:
    cue_index: int
    start_ms: int
    end_ms: int
    speaker: str | None
    text: str


@dataclass(frozen=True)
class CanonicalTranscript:
    """One canonical document's cues, exactly as stored. The AI's only input."""
    lecture_id: str
    document_id: str
    document_source_fingerprint: str
    cues: tuple
    trainer_speaker: str | None
    subject: str | None = None

    @property
    def by_index(self) -> dict:
        return {cue.cue_index: cue for cue in self.cues}

    @property
    def fingerprint(self) -> str:
        """sha256 over the ordered cue content: what the analysis actually read."""
        digest = hashlib.sha256()
        for cue in self.cues:
            digest.update(f"{cue.cue_index}\t{cue.start_ms}\t{cue.end_ms}\t"
                          f"{cue.speaker or ''}\t{cue.text}\n".encode("utf-8"))
        return digest.hexdigest()

    def is_trainer(self, speaker) -> bool:
        return bool(self.trainer_speaker) and normalize_speaker(speaker) == \
            normalize_speaker(self.trainer_speaker)


@dataclass(frozen=True)
class Candidate:
    """What the recall selector proposed. Untrusted until validated."""
    start_cue: int
    end_cue: int
    positive_speakers: tuple
    category: str | None
    reason: str | None
    confidence: float | None


@dataclass
class Moment:
    """A structurally valid candidate, with evidence rebuilt from the cues."""
    start_cue: int
    end_cue: int
    evidence_start_ms: int
    evidence_end_ms: int
    positive_speakers: list
    all_speakers: list
    trainer_included: bool
    conversation_type: str
    category: str
    exact_quote: str
    positive_quote: str
    dialogue: list
    reason: str | None
    selector_confidence: float | None
    source: str
    verifier_verdict: str = "PENDING"
    verifier_confidence: float | None = None
    verifier_detail: dict = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        return (self.evidence_end_ms - self.evidence_start_ms) / 1000.0

    def fingerprint(self, *, document_id: str, transcript_fingerprint: str,
                    policy_version: str) -> str:
        """Stable identity: same evidence under the same policy -> same moment."""
        speakers = ",".join(sorted(normalize_speaker(s) for s in self.positive_speakers))
        raw = (f"{policy_version}|{document_id}|{transcript_fingerprint}|"
               f"{self.start_cue}|{self.end_cue}|{speakers}")
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
