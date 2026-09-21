# Phase 3C2.3B: versioned multi-part seam deduplication

Fixes the one thing Phase 3C2.3A proved wrong — 61 seconds of audio represented
twice at a part boundary — and nothing else. The call-relative origin is left
exactly as the provider reports it.

## 1. No migration was needed

`lecture_transcript_documents` already carries
`UNIQUE (selection_id, source_content_sha256, parser_version)`, and
`lecture_transcript_cues` hangs off `document_id`. A new `parser_version`
therefore produces a **separate document with its own cues**, and both tables
already have a `metadata jsonb` column for the seam audit. v1 and v2 coexist
natively; **no DDL was written**.

## 2. Version identity

| | |
| --- | --- |
| parser version | `webvtt_canonical_v2_seam_dedup` |
| dedup policy | `earlier_part_wins_v1` |
| v1 (untouched) | `webvtt_canonical_v1` |

## 3. The algorithm

Parts are processed in Phase 2B selection order. Part 1 is kept whole. For each
later part, every raw cue is shifted by that part's **persisted Phase 2B
offset** and compared against `coverage_end_ms`, the maximum end already
covered by *all* earlier parts:

```
if shifted_cue.start_ms <  coverage_end_ms  -> drop  (the earlier part wins)
if shifted_cue.start_ms >= coverage_end_ms  -> keep
```

Because the comparison is against **cumulative** coverage, three or more parts
resolve two or more seams without special-casing; a short middle part cannot
rewind the boundary.

The decision is made on the provider's overlap geometry alone. There is no AI,
no embedding, no fuzzy text or speaker similarity — a test asserts the module
contains none of it. That is deliberate: 3C2.3A showed the duplicated audio was
transcribed so differently that text measures scored it *below* the
adjacent-content control, while speech density gave it away at 164.7 %.

Cues are finally sorted by `(start_ms, source_part_index, source_cue_index)` so
equal starts stay deterministic, then renumbered from 1.

## 4. Source and provenance

v2 never reads the flattened combined transcript. It reads the **selected parts
and their raw persisted artifacts**, so part provenance exists before any
decision is made. Every stored cue carries `source_part_index`,
`source_cue_index`, `source_artifact_id`, `source_provider_transcript_id`,
`applied_offset_ms`, `raw_start_ms` and `raw_end_ms`.

## 5. Boundary-straddling policy

A later-part cue that starts before `coverage_end_ms` but ends after it is
**dropped whole**. Splitting its text at a timestamp would be a guess about
where a sentence belongs. The tail that goes with it is measured, not hidden:
for Andrew, **1 straddling cue with a maximum dropped tail of 11,755 ms**. This
is the one real cost of earlier-wins and is reported rather than buried.

## 6. Andrew: v1 vs v2

| | v1 | v2 |
| --- | ---: | ---: |
| cues | 601 | **592** |
| cues removed | — | **9** (all from part 2) |
| straddling dropped / max tail | — | **1 / 11,755 ms** |
| first cue | 10,026,775 ms | **10,026,775 ms** |
| last cue | 17,195,372 ms | **17,195,372 ms** |
| duration | 7,168,597 ms (119.48 min) | **7,168,597 ms** |
| overlapping cues | 93 | **84** (the 84 legitimate within-part overlaps survive) |
| backward timeline steps | **1, of 52,845 ms** | **0** |
| speakers | 16 | 16 |
| attendance (roster v2) | 7 | 7 |
| spoke | 7 | 7 |
| engagement | 100.00 %, score 5 | 100.00 %, score 5 |

Seam audit: `earlier_coverage_end_ms 14,403,937`,
`later_shifted_first_start_ms 14,342,932`, `seam_overlap_ms 61,005`,
`later_cues_examined 251`, `dropped 9`, `kept 242`.

Speech density inside the old seam window falls from **164.7 % to 82.1 %** —
exactly one part's worth, which is what a single audio stream should show.

Downstream figures are unchanged because all nine removed cues came from
speakers already present elsewhere in the lecture, so no speaker, learner or
engagement fact depended on them. That is an outcome, not a target.

## 7. Explicit version binding

Every consumer already took a `parser_version`; the only change was to expose it:
`build-speaker-inventory --parser-version`, `resolve-speakers --parser-version`
and `run-qa-shadow --parser-version`, plus the new
`rebuild-seam-document --lecture-id`. Defaults are unchanged, so **no lecture
moved to v2 implicitly** — the platform still holds 14 v1 documents and exactly
1 v2 document.

Phase 3A can therefore select the v2 chain explicitly when Andrew is evaluated,
with `--lecture-id` scoping it to that one lecture.

## 8. What was proven

- **Duplicate coverage gone:** 0 part-2 cues start before part-1 coverage ends.
- **Chronological:** 0 backward steps, against 1 × 52,845 ms in v1.
- **Origin preserved:** first cue still 10,026,775 ms; duration still a span.
- **v1 immutable:** 601 cues, same fingerprint `158bb1a7ad9b09a9…`, same
  metrics, same 93 overlaps.
- **Raw artifacts immutable:** both at `content_version = 1`, shas unchanged.
- **Phase 2B unchanged:** one selection, `legacy_qa_v8_overlap_cluster_v1`,
  parts `[(1, 0, primary), (2, 14,336,082)]`.
- **Idempotent:** a second and third rebuild produce the identical document id,
  cue digest and seam fingerprint.
- **Single-part regression: 13/13 lectures reproduce v1 exactly**, cue for cue
  including text hashes and duration, with **zero dedup actions**.

## 9. One honest caveat

`calculate-engagement` is date-scoped, not lecture-scoped, so running it for
2026-09-16 re-upserted the other seven lectures' engagement rows. The values
are byte-identical (verified: Ray | PMP still 13/5/38.46 %, and every other
lecture matches its pre-existing figures), so this is an idempotent rewrite of
our own derived table, not a change — but it is wider than "Andrew only" and is
recorded here rather than glossed over.

## 10. Still out of scope, carried forward

- **Positive Clips:** `clip.start` → `start_seconds` → `ffmpeg -ss` passes the
  cue timestamp straight to the trimmer. Whether a recording is call-length or
  lecture-length is still **empirically unverified** (only 1 of 651 legacy
  sessions has a recording duration, and it has a ~0 origin).
- **Lecture Parts:** `planner._zone` filters
  `lo*duration <= start_seconds <= hi*duration` and therefore selects zero cues
  for any non-zero origin. Still broken; not touched here.
