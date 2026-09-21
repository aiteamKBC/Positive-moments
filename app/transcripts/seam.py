"""
Phase 3C2.3B: deterministic cross-part seam deduplication.

A Teams call can expose more than one transcript artifact, and the later
artifact's window can begin *inside* the earlier one's. Phase 3C2.3A proved
that on a real lecture (Andrew-Scheduling Professional, 2026-09-16) the shared
61 s carries the same audio twice: each part alone shows ~82 % speech density,
together 164.7 %, against a single-part control median of 89.9 %. The two
transcriptions of that audio diverge so far that no text measure recognises
them as the same speech - so the decision here is made on the provider's
overlap geometry alone, never on wording, speakers or similarity.

The legacy v8 combiner concatenates parts and never deduplicates, so canonical
v1 reproduces the duplication faithfully. This module is the versioned v2
alternative; v1 rows stay exactly as they are.

Two things this deliberately does NOT do:

- it does not touch the call-relative origin. Andrew's first cue is at
  02:47:06 because the Teams call opened 2h47 before the lecture, which is what
  the provider reports and what 103 legacy production sessions already store;
- it does not deduplicate overlaps *within* one part. Cues from a single ASR
  pass legitimately overlap, and only a later PART starting inside an earlier
  part's covered timeline is treated as duplicate coverage.
"""
import hashlib
from dataclasses import dataclass, field, replace
from typing import Any

from app.transcripts.webvtt import (
    EMPTY_TRANSCRIPT,
    PARSED,
    PARSED_WITH_WARNINGS,
    ParsedCue,
    ParsedDocument,
    parse_combined_webvtt,
)


SEAM_PARSER_VERSION = "webvtt_canonical_v2_seam_dedup"
DEDUP_POLICY_VERSION = "earlier_part_wins_v1"


@dataclass(frozen=True)
class TranscriptPart:
    """One selected Phase 2B part, with its persisted offset and raw content."""
    part_index: int
    offset_ms: int
    content: str
    artifact_id: str | None = None
    provider_transcript_id: str | None = None


@dataclass
class SeamAudit:
    """Everything needed to explain one part boundary, without cue text."""
    earlier_part_index: int
    later_part_index: int
    earlier_artifact_id: str | None
    later_artifact_id: str | None
    earlier_coverage_end_ms: int
    later_raw_first_start_ms: int | None
    later_shifted_first_start_ms: int | None
    later_offset_ms: int
    seam_overlap_ms: int
    later_cues_examined: int
    dropped_cue_count: int
    boundary_straddling_dropped_count: int
    maximum_dropped_tail_ms: int
    kept_cue_count: int

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "dedup_policy_version": DEDUP_POLICY_VERSION}


@dataclass
class SeamDocument:
    """The v2 parse result: canonical cues plus the seam audit."""
    status: str
    cues: list[ParsedCue] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    seams: list[SeamAudit] = field(default_factory=list)

    @property
    def dropped_cue_count(self) -> int:
        return sum(seam.dropped_cue_count for seam in self.seams)


def parse_parts_with_seam_dedup(parts) -> SeamDocument:
    """
    Parse each selected part on its own, shift it by its persisted Phase 2B
    offset, and drop a later part's cues that start inside the timeline the
    earlier parts already cover.

    Parts are processed in Phase 2B selection order. Any number of parts is
    supported: each later part is compared against the CUMULATIVE coverage end
    of everything before it, so a three-part selection resolves two seams.
    """
    ordered = sorted(parts, key=lambda part: part.part_index)
    if not ordered:
        return SeamDocument(EMPTY_TRANSCRIPT, metrics=_empty_metrics())

    kept: list[ParsedCue] = []
    warnings: list[dict[str, Any]] = []
    seams: list[SeamAudit] = []
    per_part_status: list[str] = []
    coverage_end_ms: int | None = None
    previous = None

    for part in ordered:
        parsed = parse_combined_webvtt(part.content)
        per_part_status.append(parsed.status)
        # Part-level warnings keep their own provenance so a malformed block in
        # part 2 is never mistaken for one in part 1.
        warnings += [{**warning, "part_index": part.part_index}
                     for warning in parsed.warnings]

        shifted = [_shift(cue, part) for cue in parsed.cues]

        if coverage_end_ms is None:
            # The first part is always kept whole: there is nothing before it.
            kept += shifted
            if shifted:
                coverage_end_ms = max(cue.end_ms for cue in shifted)
            previous = part
            continue

        survivors, audit = _apply_earlier_wins(
            shifted, coverage_end_ms, part, previous)
        seams.append(audit)
        if audit.dropped_cue_count:
            warnings.append({
                "code": "SEAM_DUPLICATE_COVERAGE_DROPPED",
                "part_index": part.part_index,
                "dropped_cue_count": audit.dropped_cue_count,
                "seam_overlap_ms": audit.seam_overlap_ms,
            })
        if audit.boundary_straddling_dropped_count:
            warnings.append({
                "code": "SEAM_BOUNDARY_STRADDLING_CUE_DROPPED",
                "part_index": part.part_index,
                "count": audit.boundary_straddling_dropped_count,
                "maximum_dropped_tail_ms": audit.maximum_dropped_tail_ms,
            })
        kept += survivors
        if survivors:
            coverage_end_ms = max(coverage_end_ms,
                                  max(cue.end_ms for cue in survivors))
        previous = part

    # Chronological by start, then by the original part order and raw ordinal,
    # so equal starts stay deterministic.
    kept.sort(key=lambda cue: (cue.start_ms,
                               cue.metadata["source_part_index"],
                               cue.metadata["source_cue_index"]))
    renumbered = [replace(cue, cue_index=position)
                  for position, cue in enumerate(kept, start=1)]

    metrics = _metrics(renumbered, seams, len(ordered))
    status = _status(per_part_status, warnings, renumbered)
    return SeamDocument(status, cues=renumbered, metrics=metrics,
                        warnings=warnings, seams=seams)


def _shift(cue: ParsedCue, part: TranscriptPart) -> ParsedCue:
    """Apply the persisted Phase 2B offset and record where the cue came from."""
    return replace(
        cue,
        start_ms=cue.start_ms + part.offset_ms,
        end_ms=cue.end_ms + part.offset_ms,
        metadata={
            **cue.metadata,
            "source_part_index": part.part_index,
            "source_cue_index": cue.cue_index,
            "source_artifact_id": part.artifact_id,
            "source_provider_transcript_id": part.provider_transcript_id,
            "applied_offset_ms": part.offset_ms,
            "raw_start_ms": cue.start_ms,
            "raw_end_ms": cue.end_ms,
        },
    )


def _apply_earlier_wins(shifted, coverage_end_ms: int, part, previous):
    """
    Drop the later part's cues that begin inside the covered timeline.

    A cue that straddles the boundary - starting before `coverage_end_ms` but
    ending after it - is dropped WHOLE. Splitting its text at a timestamp would
    be a heuristic guess about where a sentence belongs, so the tail it takes
    with it is measured and reported rather than silently discarded.
    """
    survivors, dropped, straddling, max_tail = [], 0, 0, 0
    for cue in shifted:
        if cue.start_ms < coverage_end_ms:
            dropped += 1
            if cue.end_ms > coverage_end_ms:
                straddling += 1
                max_tail = max(max_tail, cue.end_ms - coverage_end_ms)
            continue
        survivors.append(cue)
    first = shifted[0] if shifted else None
    return survivors, SeamAudit(
        earlier_part_index=previous.part_index if previous else 0,
        later_part_index=part.part_index,
        earlier_artifact_id=previous.artifact_id if previous else None,
        later_artifact_id=part.artifact_id,
        earlier_coverage_end_ms=coverage_end_ms,
        later_raw_first_start_ms=first.metadata["raw_start_ms"] if first else None,
        later_shifted_first_start_ms=first.start_ms if first else None,
        later_offset_ms=part.offset_ms,
        seam_overlap_ms=max(0, coverage_end_ms - first.start_ms) if first else 0,
        later_cues_examined=len(shifted),
        dropped_cue_count=dropped,
        boundary_straddling_dropped_count=straddling,
        maximum_dropped_tail_ms=max_tail,
        kept_cue_count=len(survivors),
    )


def _empty_metrics() -> dict[str, Any]:
    return {"cue_count": 0, "empty_text_cue_count": 0, "overlapping_cue_count": 0,
            "malformed_block_count": 0, "multi_speaker_cue_count": 0,
            "non_monotonic_cue_count": 0, "zero_length_cue_count": 0,
            "unique_raw_speaker_label_count": 0, "cue_identifier_count": 0,
            "other_markup_tag_count": 0, "seam_dropped_cue_count": 0,
            "seam_count": 0, "part_count": 0}


def _metrics(cues, seams, part_count: int) -> dict[str, Any]:
    """
    Recomputed over the FINAL cue list, so every count describes what was
    actually stored rather than what any single part contained.
    """
    metrics = _empty_metrics()
    metrics["part_count"] = part_count
    metrics["seam_count"] = len(seams)
    metrics["seam_dropped_cue_count"] = sum(s.dropped_cue_count for s in seams)
    metrics["seam_boundary_straddling_dropped_count"] = sum(
        s.boundary_straddling_dropped_count for s in seams)
    metrics["seam_maximum_dropped_tail_ms"] = max(
        (s.maximum_dropped_tail_ms for s in seams), default=0)
    if not cues:
        return metrics

    labels = set()
    previous_end = previous_start = None
    for cue in cues:
        metrics["cue_count"] += 1
        if cue.speaker_label_raw:
            labels.add(cue.speaker_label_raw)
        if cue.metadata.get("empty_text"):
            metrics["empty_text_cue_count"] += 1
        if cue.metadata.get("multiple_speaker_labels"):
            metrics["multi_speaker_cue_count"] += 1
        if cue.metadata.get("cue_identifier"):
            metrics["cue_identifier_count"] += 1
        if cue.metadata.get("other_markup_tags"):
            metrics["other_markup_tag_count"] += 1
        if cue.end_ms == cue.start_ms:
            metrics["zero_length_cue_count"] += 1
        if previous_end is not None and cue.start_ms < previous_end:
            # Legitimate within-part overlap; counted, never removed.
            metrics["overlapping_cue_count"] += 1
        if previous_start is not None and cue.start_ms < previous_start:
            metrics["non_monotonic_cue_count"] += 1
        previous_end = cue.end_ms if previous_end is None else max(previous_end, cue.end_ms)
        previous_start = cue.start_ms

    metrics["unique_raw_speaker_label_count"] = len(labels)
    metrics["first_cue_start_ms"] = cues[0].start_ms
    metrics["last_cue_end_ms"] = max(cue.end_ms for cue in cues)
    # Duration stays a span of the call-relative timeline, never max-from-zero.
    metrics["duration_ms"] = metrics["last_cue_end_ms"] - metrics["first_cue_start_ms"]
    return metrics


def _status(per_part_status, warnings, cues) -> str:
    if not cues:
        return EMPTY_TRANSCRIPT
    if any(status not in (PARSED, PARSED_WITH_WARNINGS) for status in per_part_status):
        # A part that could not be parsed is a real failure, not a warning.
        return next(status for status in per_part_status
                    if status not in (PARSED, PARSED_WITH_WARNINGS))
    return PARSED_WITH_WARNINGS if warnings else PARSED


def seam_fingerprint(parts, *, policy_version: str = DEDUP_POLICY_VERSION) -> str:
    """
    Deterministic fingerprint of the v2 inputs: the ordered parts, their
    offsets and their exact content, plus the policy that combined them.
    """
    digest = hashlib.sha256()
    digest.update(f"policy:{policy_version}\n".encode())
    for part in sorted(parts, key=lambda item: item.part_index):
        digest.update(
            f"part:{part.part_index}|offset:{part.offset_ms}|"
            f"artifact:{part.artifact_id}|"
            f"sha:{hashlib.sha256((part.content or '').encode()).hexdigest()}\n".encode())
    return digest.hexdigest()
