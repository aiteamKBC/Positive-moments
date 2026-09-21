"""
The legacy evidence-text renderer, ported from `Build Session and Checklist
Rows` (`normSpeaker`, `clipText`, `blocksFromRange`, `formatClips`).

One deliberate change of INPUT, not of behaviour: legacy re-parsed the WebVTT
inside the QA node, while this renderer reads the canonical Phase 2C1 cues.
The cues are the same parsed evidence, already validated, so the renderer stays
reproducible even if the WebVTT parser later changes - and it never re-parses
a transcript.

Cue timestamps are reformatted from milliseconds with the same
`format_timestamp` the combined transcript was written with, so the rendered
`HH:MM:SS.mmm` strings are the ones legacy would have read out of the VTT.

Every rendered block keeps the cue ids it came from, so any quote can be traced
back to specific canonical cues without duplicating transcript text.
"""
import re

from app.transcripts.combine import format_timestamp


RENDERER_VERSION = "legacy_qa_v8_renderer_v1"

# Legacy constants, verbatim from formatClips.
MAX_BLOCKS_PER_CLIP = 8
MAX_CHARS_PER_BLOCK = 260
# Legacy blocksFromRange merge window.
MAX_BLOCK_GAP_MS = 1500

NO_EVIDENCE = "No evidence found"
NO_EXTRACTABLE_FOR_RANGE = "No extractable quote for this range"
NO_EXTRACTABLE = "No extractable quote"

_WHITESPACE = re.compile(r"\s+")


def norm_speaker(value) -> str:
    """
    Legacy `normSpeaker`: trim then lowercase, and nothing else.

    This is a rendering comparison only - it decides whether two adjacent cues
    belong to the same speaker block. It is NOT person matching, and no fuzzy
    comparison belongs here.
    """
    return str(value or "").strip().lower()


def clip_text(text, max_chars: int = MAX_CHARS_PER_BLOCK) -> str:
    """Legacy `clipText`: collapse whitespace, trim, truncate with an ellipsis."""
    collapsed = _WHITESPACE.sub(" ", str(text or "")).strip()
    if not collapsed:
        return ""
    if len(collapsed) > max_chars:
        return collapsed[:max_chars].strip() + "..."
    return collapsed


def blocks_from_range(start_ms: int, end_ms: int, cues) -> list[dict]:
    """
    Legacy `blocksFromRange`, over canonical cues.

    Selects cues overlapping the range (`cue.start < end AND cue.end > start`),
    drops empty text, sorts chronologically, then merges consecutive cues into
    one block while the speaker matches and the gap since the block's last cue
    end is 1500 ms or less.
    """
    if start_ms is None or end_ms is None or end_ms <= start_ms:
        return []
    overlaps = sorted(
        (cue for cue in cues
         if cue["start_ms"] < end_ms and cue["end_ms"] > start_ms
         and str(cue.get("text") or "").strip()),
        key=lambda cue: (cue["start_ms"], cue["end_ms"], cue["cue_index"]))
    if not overlaps:
        return []

    blocks: list[dict] = []
    current = None
    for cue in overlaps:
        speaker = cue.get("speaker_label_raw") or ""
        if current is None:
            current = _new_block(cue, speaker)
            continue
        same_speaker = norm_speaker(current["speaker"]) == norm_speaker(speaker)
        small_gap = (cue["start_ms"] - current["last_end_ms"]) <= MAX_BLOCK_GAP_MS
        if same_speaker and small_gap:
            current["end_ms"] = cue["end_ms"]
            current["texts"].append(cue["text"])
            current["cue_ids"].append(cue["cue_id"])
            current["last_end_ms"] = cue["end_ms"]
        else:
            blocks.append(current)
            current = _new_block(cue, speaker)
    if current is not None:
        blocks.append(current)
    return blocks


def _new_block(cue, speaker) -> dict:
    return {"start_ms": cue["start_ms"], "end_ms": cue["end_ms"], "speaker": speaker,
            "texts": [cue["text"]], "cue_ids": [cue["cue_id"]],
            "last_end_ms": cue["end_ms"]}


def format_clips(clips, cues) -> dict:
    """
    Legacy `formatClips`, plus the provenance legacy never kept.

    Returns the rendered text exactly as legacy produced it, and alongside it
    the blocks with their source cue ids so every quote is traceable.

    `clips` are validated Phase 3A evidence clips in their stored order; each
    needs `start_ms`, `end_ms` and the original `start_text` / `end_text`,
    because the "no extractable quote for this range" line prints the model's
    own timestamp strings.
    """
    if not clips:
        return {"text": NO_EVIDENCE, "blocks": [], "block_count": 0,
                "cue_ids": [], "clip_ids": []}

    lines: list[str] = []
    rendered_blocks: list[dict] = []
    for clip_index, clip in enumerate(clips, start=1):
        start_text = clip.get("start_text") or ""
        end_text = clip.get("end_text") or ""
        if not start_text or not end_text:
            # Legacy skipped a clip missing either endpoint entirely.
            continue
        blocks = blocks_from_range(clip.get("start_ms"), clip.get("end_ms"), cues)
        if not blocks:
            lines.append(f"[{clip_index}] {start_text} --> {end_text}: "
                         f"{NO_EXTRACTABLE_FOR_RANGE}")
            continue
        for block_index, block in enumerate(blocks[:MAX_BLOCKS_PER_CLIP], start=1):
            speaker = str(block["speaker"] or "").strip()
            speaker_text = f" ({speaker})" if speaker else ""
            quote = clip_text(" ".join(block["texts"])) or NO_EXTRACTABLE
            line = (f"[{clip_index}.{block_index}] {format_timestamp(block['start_ms'])} --> "
                    f"{format_timestamp(block['end_ms'])}{speaker_text}: {quote}")
            lines.append(line)
            rendered_blocks.append({
                "clip_id": clip.get("clip_id"), "clip_index": clip_index,
                "block_index": block_index, "start_ms": block["start_ms"],
                "end_ms": block["end_ms"], "cue_ids": block["cue_ids"],
                "speaker_present": bool(speaker), "line": line,
            })
    text = "\n".join(lines) if lines else NO_EVIDENCE
    return {
        "text": text, "blocks": rendered_blocks, "block_count": len(rendered_blocks),
        "cue_ids": [cue_id for block in rendered_blocks for cue_id in block["cue_ids"]],
        "clip_ids": [clip.get("clip_id") for clip in clips],
    }


def legacy_evidence_field(rendered_evidence: str, reasoning) -> str:
    """
    The legacy `Upsert QA Checklist Items` evidence expression, verbatim:

        evidence === "No evidence found"
            ? (reasoning || "")
            : evidence + (reasoning ? "\\n" + reasoning : "")

    The platform keeps `rendered_evidence` and `reasoning` as separate columns
    too; this is only the single-field compatibility representation.
    """
    text = rendered_evidence or ""
    note = reasoning or ""
    if text == NO_EVIDENCE:
        return note
    return f"{text}\n{note}" if note else text
