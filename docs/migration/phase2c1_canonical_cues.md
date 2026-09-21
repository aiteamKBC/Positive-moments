# Phase 2C1 — canonical transcript document and cue model

Status: **PHASE_2C1_CANONICAL_CUES_READY**

Validation date: 2026-09-16. Target Cairo business date: 2026-09-04.

Phase 2C1 converts the Phase 2B derived combined transcript into a canonical,
structured cue representation and stops there. No speaker normalization,
attendance, engagement, QA, AI, Positive Clips, or Lecture Split work happened.

## Pipeline

```
lecture_combined_transcripts   (read-only Phase 2B evidence)
  -> canonical WebVTT parse
  -> lecture_transcript_documents
  -> lecture_transcript_cues
  -> lecture_transcript_parse_runs   (audit)
```

Zero Microsoft Graph calls, no re-listing, no re-download, no reselection, and
no reconstruction of the combined transcript from raw artifacts.

## Existing parsers inspected, and the reuse decision

| Location | Verdict |
| --- | --- |
| `automation/lecture_parts/planner.parse_webvtt` | Real, production Python parser. **Not reused as canonical, not modified.** |
| `app/transcripts/combine.py` (Phase 2B) | Timing/header primitives **reused** by the canonical parser. |
| Positive Clips | SQL/n8n orchestration only; no Python WebVTT parser to reuse. |

`planner.parse_webvtt` feeds the AI split planner and makes choices that are
right there and wrong for a canonical evidence layer: it **drops empty-text
cues** (which renumbers cue IDs and loses evidence), raises instead of returning
a status, and reports no malformed, overlap, or multi-speaker diagnostics. It is
left completely untouched, and the Lecture Parts pipeline keeps using it.

To avoid a second competing timestamp convention inside the new platform, the
canonical parser imports `TIMING`, `HEADER` and `timestamp_to_ms` from the
Phase 2B module, so `app/` has exactly one WebVTT timing implementation.

## Schema

**`lecture_transcript_documents`** — `document_id` PK; FKs to `lecture_sessions`,
`lecture_transcript_selections`, `lecture_combined_transcripts`;
`parser_version`, `source_fingerprint`, `source_content_sha256`; `parse_status`;
`cue_count`, `first_cue_start_ms`, `last_cue_end_ms`, `duration_ms`;
`unique_raw_speaker_label_count`, `empty_text_cue_count`,
`overlapping_cue_count`, `malformed_block_count`, `multi_speaker_cue_count`,
`warning_count`; `combined_duration_seconds`, `duration_difference_ms`;
timestamps; `metadata jsonb`.
Unique `(selection_id, source_content_sha256, parser_version)`; CHECKs on hash
shape, non-negative counts, and `duration_ms = last - first`.

**`lecture_transcript_cues`** — `cue_id` PK (uuidv5 of document + index);
`document_id` FK ON DELETE CASCADE; `cue_index`; `start_ms`, `end_ms`;
`speaker_label_raw` (nullable); `text`; `cue_text_sha256`; `metadata jsonb`.
Unique `(document_id, cue_index)`; CHECKs `cue_index > 0`, `start_ms >= 0`,
`end_ms >= start_ms`.

**`lecture_transcript_parse_runs`** — audit counters only, no transcript text.

## Identity and versioning

```
document_id = uuidv5(ns, "selection:{selection_id}\0content:{sha}\0parser:{version}")
```

Deliberately **not** keyed by lecture. A reselection, changed combined bytes, or
a new parser version each produce a **new** document row; the previous document
and its cues stay exactly as they were. Versions coexist — nothing is migrated
or overwritten — and a consumer picks the document whose `selection_id` and
`source_content_sha256` match the current `lecture_combined_transcripts` row.

Cue writes are idempotent within a document: when the stored document already
matches the parse exactly (same status, same cue count, same number of stored
cues), the cues are left untouched; otherwise they are replaced wholesale so a
reparse can never leave a stale tail.

## Source fingerprint

`source_fingerprint` is carried **verbatim** from
`lecture_combined_transcripts.source_fingerprint` (Phase 2B: SHA-256 over the
selection version and the ordered source artifact hashes). No second convention
was invented. `source_content_sha256` is additionally stored so a document is
traceable to the exact bytes parsed, not just to the inputs that produced them.

## Parser

`app/transcripts/webvtt.py`, version **`webvtt_canonical_v1`**.

Rules: single `WEBVTT` header (BOM tolerated) or `INVALID_WEBVTT`; blocks split
on blank lines; optional cue identifier line kept as provenance; `HH:MM:SS.mmm`
with hours past 23 and `.` or `,` milliseconds; cue settings after the
timestamps preserved; multi-line cue text joined with `\n`; `NOTE`/`STYLE`/
`REGION` blocks skipped rather than counted as malformed; whole-millisecond
integer offsets on the combined timeline, timezone-free.

Text handling removes only WebVTT markup and decodes HTML entities. Wording is
never summarized, rewritten, spell-corrected, translated, or trimmed. Non-voice
tags are stripped but their names recorded in cue metadata, so nothing vanishes
without trace. `cue_text_sha256` hashes the canonical text.

## Voice tags

One distinct `<v …>` label → `speaker_label_raw`. No label → `NULL`.
**Conflicting labels in one cue → `NULL`**, plus
`metadata.multiple_speaker_labels` listing every label seen and a
`MULTIPLE_SPEAKER_LABELS` warning. Picking one would invent a speaker; all
utterance text is still preserved in `text`. Only the raw provider label is
stored — no LMS, attendance, trainer/learner, alias, or percentage logic exists
at this layer, and the cue table has no identity columns.

## Malformed input

Statuses: `PARSED`, `PARSED_WITH_WARNINGS`, `EMPTY_TRANSCRIPT`,
`INVALID_WEBVTT`, `UNSUPPORTED_STRUCTURE`, `ERROR`. Status is derived purely
from the warning list, so `warning_count` can never disagree with it.

A block with no timing line is `BLOCK_WITHOUT_TIMING`; an end before its start
is `END_BEFORE_START` and is **not stored and not "repaired"** by swapping
timestamps. Empty-text cues are **kept**, counted, and warned — the opposite of
the legacy parser's silent drop.

**Overlapping cues are not a warning.** Two people talking over each other is
ordinary speaker-attributed Teams output; overlaps are counted, flagged on the
cue, and never altered. An earlier draft treated them as warnings, which marked
all seven real transcripts `PARSED_WITH_WARNINGS` with `warning_count = 0` —
corrected.

## Real results, 2026-09-04

| Lecture | Status | Cues | First (ms) | Last (ms) | Parsed (ms) | 2B (s) | Diff | Labels | Overlaps | Warn |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| AI in Project Control 2026 | PARSED | 102 | 82,040 | 854,429 | 772,389 | 772 | **0** | 7 | 6 | 0 |
| Femi-Commercial Intelligence-Oct 25 | PARSED | 668 | 138,913 | 8,774,313 | 8,635,400 | 8,635 | **0** | 10 | 69 | 0 |
| G2-Juliane -Impact and Planning June 2026 | PARSED | 770 | 17,472 | 7,985,432 | 7,967,960 | 7,968 | **0** | 9 | 70 | 0 |
| Project Planning & Control (PPC) \| Andrew | PARSED | 235 | 8,585 | 12,594,682 | 12,586,097 | 12,586 | **0** | 8 | 14 | 0 |
| Ray-Project Management Office (PMO) | PARSED | 586 | 127,272 | 7,488,660 | 7,361,388 | 7,361 | **0** | 8 | 58 | 0 |
| G3 - Femi - Customer Journey Optimisation | PARSED | 728 | 39,650 | 7,549,327 | 7,509,677 | 7,510 | **0** | 9 | 101 | 0 |
| Ray-MSP Jan 2026 | PARSED | 861 | 14,534 | 7,486,113 | 7,471,579 | 7,472 | **0** | 17 | 138 | 0 |

**3,950 cues. Duration parity exact — 0 ms difference on every lecture.** Zero
empty-text, malformed, multi-speaker, or zero-length cues. 66 distinct raw
speaker labels overall; every cue carries a label. 356 overlapping cues,
preserved unchanged.

### Diagnostic comparison with the Lecture Parts parser

Read-only, against the identical stored combined transcripts, writing nothing:
**3,950 vs 3,950 cues, delta 0 on every lecture**, identical first-start and
last-end times and identical speaker labels. The two parsers agree exactly on
this data, which contains no empty-text cues — the one case where they would
legitimately diverge.

## Idempotency

| Metric | Run 1 | Run 2 |
| --- | ---: | ---: |
| Documents created | 7 | **0** |
| Documents reused | 0 | 7 |
| Cues written | 3,950 | **0** |
| Cues reused | 0 | 3,950 |

Identical document IDs, cue counts, statuses, source hashes and durations. In
the database: 0 duplicate provenance rows, 0 duplicate cue indexes, 0 orphan
cues, and all 3,950 `cue_text_sha256` values re-verified against the stored text
by PostgreSQL itself.

## Carried production-cutover risks — NOT resolved

These remain open from Phase 2B and are **not** addressed by Phase 2C1:

1. **REAL MULTIPART FIXTURE VALIDATED = NO.** No real multi-part occurrence has
   been validated; the multi-part selection and combination rules are covered
   only by synthetic fixtures.
2. **Legacy Master vs One Lecture timezone divergence not exercised.** The QA
   Master renders the calendar in Africa/Cairo while the legacy One Lecture node
   parsed those strings as UTC. The platform's reading is the correct one, but
   no real date with several same-day candidates has yet been able to tell the
   two readings apart.

Both are production-cutover gates.

## Phase 2C2 entry point

Start from `lecture_transcript_cues.speaker_label_raw`, never from the combined
text or the raw artifacts. First change: an additive speaker-identity layer
(for example `lecture_transcript_speakers`) keyed by `document_id` plus the
distinct raw label, resolving label → normalized identity as its own evidence
table with its own provenance and version. Leave `lecture_transcript_cues`
immutable: 2C2 should join to a speaker table rather than add identity columns
to the cue rows, so a changed matching algorithm never rewrites parsed cues.
Attendance, engagement, and QA stay out of 2C2.
