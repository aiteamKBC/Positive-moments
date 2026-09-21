# Phase 3B: legacy QA output rendering and LMS snapshot

```
lecture_qa_evaluations + _checklist_items + _evidence_clips   (Phase 3A)
lecture_transcript_cues                                       (canonical evidence)
lecture_attendance_snapshots / engagement                     (Item 7)
kbc_users_data  ->  lecture_lms_snapshots (frozen, read-only source)
        │
        ▼
lecture_qa_rendered_sessions + lecture_qa_rendered_checklist_items
        │
        ▼
Phase 3C writer cutover (not started)
```

A renderer, never an evaluator. **No model is ever called** — the service has
no provider at all, and `lecture_qa_render_runs.provider_calls` is
CHECK-constrained to zero. Output is shadow only; no `qa_doctors_*` table is
written or viewed over.

Renderer **`legacy_qa_v8_renderer_v1`**; LMS snapshot
**`legacy_qa_v8_active_lms_roster_v1`**.

## 1. Legacy sources

| Node | What was taken |
| --- | --- |
| `Build Session and Checklist Rows` | `normSpeaker`, `clipText`, `blocksFromRange`, `formatClips`, `titledItemsToObject`, `ksbsToObject`, the Item 7 evidence sentences, `date`, severity, the met/partial/not-met counts |
| `Upsert QA Session` | the 22 mapped session fields |
| `Upsert QA Checklist Items` | `session_id_match`, and the evidence/reasoning expression |
| `Get LMS Students` | the full SQL contract |

## 2. Evidence rendering

Cue selection is the legacy overlap test — `cue.start < clip.end AND
cue.end > clip.start` — with empty-text cues dropped and the rest sorted
chronologically. Consecutive cues merge into one speaker block while the
speaker matches and the gap since the block's last cue end is **≤ 1500 ms**.
Speaker comparison is trim + lowercase only; no fuzzy matching belongs in a
renderer.

Constants, verbatim: **`MAX_BLOCKS_PER_CLIP = 8`**,
**`MAX_CHARS_PER_BLOCK = 260`**, merge gap **1500 ms**. A quote is whitespace
-collapsed, trimmed, and truncated with `...`.

Line format:

```
[clip.block] HH:MM:SS.mmm --> HH:MM:SS.mmm (Speaker): quote
[clip.block] HH:MM:SS.mmm --> HH:MM:SS.mmm: quote          (no speaker)
[clip] start --> end: No extractable quote for this range  (range hits nothing)
No evidence found                                          (no usable clips)
```

**Input changed, behaviour did not.** Legacy re-parsed the WebVTT inside the QA
node; this renderer reads the canonical Phase 2C1 cues, which are the same
parsed evidence, already validated. Timestamps are re-formatted from
milliseconds with the same `format_timestamp` the combined transcript was
written with, so the strings match what legacy read out of the VTT. Nothing
re-parses a transcript, and canonical cues are never modified.

Only clips whose Phase 3A `validation_status` is `VALID` are rendered. An
evaluation carrying any other status is reported as `INCONSISTENT_EVIDENCE`
rather than rendered.

The single legacy evidence field is reproduced exactly:

```
evidence === "No evidence found" ? (reasoning || "")
                                 : evidence + (reasoning ? "\n" + reasoning : "")
```

and `rendered_evidence`, `reasoning`, `ai_status`, `status_source`,
`evidence_clip_ids` and `cue_ids` are all kept separately, so the one-field
legacy shape loses no provenance.

**Item 7** is rebuilt from the persisted Phase 2C3 snapshot and Phase 2C4
result — never from live attendance:

```
Engagement score = {spoke} / {attended} = {percent}%.
Students who spoke ({n}): ...
Students who attended but did not speak ({n}): ...
```

Who spoke is listed by transcript speaker label, loudest first, as legacy did.
Legacy emitted the silent list in raw database order, which is not
reproducible, so it is ordered deterministically by name. Those names appear
only in the rendered output column — never in run metadata or logs.

## 3. LMS snapshot

The legacy query, ported: normalize both the module and `kbc_users_data."Group"`
with `replace('&amp;','&')` → collapse whitespace → trim → lower; require exact
equality; require `lower(coalesce("Program-Status",'')) = 'active'`; order by
`FullName` and cap at **500 rows**; then take distinct `(ID, FullName)` pairs
with a non-null id and a non-blank name. `lms_module` is the **raw** module
string, and `lms_students` is `{"students":[{"ID","FullName"}]}` ordered by
`FullName`.

Note the cap's position: legacy applies `LIMIT 500` to the *filtered* rows
before deduplication, so it is a source-row cap, not a student cap. That is
reproduced, and `row_cap_reached` records whether it bit.

Because the table is live, the roster is frozen into
`lecture_lms_snapshots` keyed by a fingerprint over the version, the normalized
module and the sorted `id|name` lines. An unchanged roster reuses the snapshot;
a changed one creates new provenance and leaves the old rows intact. **Once a
snapshot exists, normal rendering never queries `kbc_users_data` again** —
only an explicit `--refresh-lms` re-reads it. Only the two fields the legacy
query returned are stored; no email and no other column.

## 4. Compatibility shape

`lecture_id` stays canonical. For compatibility only, `session_id` is the
primary provider transcript id and `session_id_match` is
`session_id + "_" + checklist_order`.

`legacy_date` reproduces legacy's `String(createdDateTime).slice(0,10)` — the
**UTC** date of the selected transcript's start, not the Cairo business date.
The canonical `session_date` is stored beside it, and the metadata records
whether they agree. On 2026-09-04 all seven agree.

`duration` is the persisted legacy text (`"2 hours"`, `"2 hours 24 minutes"`),
never recomputed from transcript text. met/partial/not-met come from the
**final** statuses, after the deterministic Item 1, 2 and 7 overrides.

**Item 2 stays corrected.** The rendered status is the Phase 3A
timezone-aware value; the historical legacy status is carried separately as
`legacy_historical_item2_status` in the parity report. The two are never
merged, and the legacy 3-hour defect is not reintroduced.

## 5. Real results — 2026-09-04

Seven evaluations rendered, **0 provider calls**.

| Lecture | Status | Clips | Blocks | Rows | Evidence strings | LMS |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| AI in Project Control 2026 | RENDERED_NON_DELIVERED | 0 | 0 | 11 | 11 | — |
| Femi-Commercial | RENDERED | 43 | 191 | 11 | 9 | 16 |
| G2-Juliane | RENDERED | 45 | 184 | 11 | 9 | 1 |
| PPC \| Andrew | RENDERED | 46 | 65 | 11 | 9 | 9 |
| Ray-PMO | RENDERED | 32 | 63 | 11 | 9 | 13 |
| G3 Femi | RENDERED | 48 | 142 | 11 | 9 | 21 |
| Ray-MSP | RENDERED | 41 | 160 | 11 | 9 | 27 |
| **Total** | | **255** | **805** | **77** | | **87 members** |

Nine evidence strings per delivered lecture: items 1 and 2 are
metadata-authoritative and the prompt requires empty clips for them, so they
render as "No evidence found" and fall back to their reasoning.

**Reproducibility proof.** All 141 stored evidence strings (60 checklist rows
plus 81 strength/area/KSB/teaching-quality sections) were re-rendered from
their own validated clips and canonical cues and matched **exactly**. All 312
block lines parse to the legacy format, every one carries a speaker, and none
exceeds the 260-character cap. Zero cue references are orphaned.

## 6. Legacy parity

| Field | Class | Parity |
| --- | --- | ---: |
| session_id, meeting_id, subject, trainer | DETERMINISTIC | **6 / 6** each |
| date, duration, duration_score | DETERMINISTIC | **6 / 6** each |
| Engagement, engagement_score, cancelled_session | DETERMINISTIC | **6 / 6** each |
| lms_module | LIVE_SOURCE_DRIFT_POSSIBLE | **6 / 6** |
| lms_students_count | LIVE_SOURCE_DRIFT_POSSIBLE | 3 / 6 |
| teaching_quality_rating | AI_GENERATED | **6 / 6** |
| met / partial / not-met | AI_GENERATED | 2 / 6, 5 / 6, 1 / 6 |
| Item 2 | KNOWN_LEGACY_DEFECT | 1 / 6, by design |

Checklist structure: **6/6** lectures render eleven rows, **6/6** item strings
match legacy, **6/6** `session_id_match` values match. Status agreement is
60/66 and exact evidence-text agreement is 9/66 — both expected, because the
evidence comes from a fresh gpt-5.2 run with different clip choices and Item 2
is deliberately corrected.

**LMS drift** (live source, not a defect): three lectures match exactly
(16/16, 1/1, 21/21) and three drift (9 vs 10, 13 vs 15, 27 vs 28). The roster
was read today; the legacy values were written on 2026-09-04. Drift is reported
by counts and learner ids only.

## 7. Still open

1. **Real multi-part fixture validated: NO.**
2. **Master vs One Lecture timezone divergence** untested on a real ambiguous
   multi-candidate transcript-selection occurrence.
3. **`INVALID_EVIDENCE` retry policy** is still retryable in Phase 3A, carried
   unchanged to Phase 3C.

Production cutover readiness is not claimed.

## 8. Phase 3C entry point

Everything a writer needs is now shadow-rendered and keyed the legacy way.
Phase 3C should decide, in order:

- whether to write `qa_doctors_sessions` / `qa_doctors_checklist_items` by
  upserting on `session_id` and `session_id_match`, and behind what guard;
- what to do about Item 2, since writing the corrected value changes six
  historical rows — that is a business decision, not a technical one;
- whether LMS drift should block a write or be recorded;
- whether `INVALID_EVIDENCE` stays retryable;
- Perfect Lecture and the recording-link lookup, both untouched so far.
