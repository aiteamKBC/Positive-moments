# Phase 2C reference: legacy speaker, trainer and engagement logic

**Documentation and classification only.** Nothing beyond deterministic speaker
aggregation is implemented. This document exists so the remaining legacy
behaviours are ported deliberately, in the right phase, rather than absorbed by
accident.

Authoritative export (sanitized, in-repo):
`automation/legacy_n8n/QA_One_Lecture_Safe_Exact_Recording_v8.json`

Relevant nodes: `Build Session and Checklist Rows` (the speaker, trainer and
engagement logic) and `Aggregate Attendees` (the attendance source). The
`QA Master Daily — Safe Exact Recording v8` export contributes nothing to
speaker derivation; it supplies the lecture input contract only, audited in
[phase2_transcript_selection_reference.md](phase2_transcript_selection_reference.md).

Only behaviour actually present in those exports is recorded below. Nothing is
guessed.

## Classification

| # | Legacy behaviour | Export location | Category | Phase |
| --- | --- | --- | --- | --- |
| 1 | Sum cue seconds per raw `<v>` label | `Build Session…` L248-254 | **deterministic speaker aggregation** | **2C2 — done** |
| 2 | Sort speakers by total speech, take the top one as `lecturerFromVtt` | L256-257 | role inference | 2C3 |
| 3 | `rawStudentSpeakers` = every other speaker | L259-261 | role inference | 2C3 |
| 4 | Attendance rows from `public.kbc_attendance` via the Attendance subflow | `Aggregate Attendees` | attendance-dependent | 2C3 |
| 5 | Bot/AI-notetaker exclusion list | `Aggregate Attendees` | attendance-dependent filtering | 2C3 |
| 6 | Dedup attendees by `name\|email` | `Aggregate Attendees` | person matching | 2C3 |
| 7 | `isSimilar` — Levenshtein ≥ 0.8 plus token-subset with ≥3-char prefixes | L171-243 | person matching (fuzzy) | 2C3 |
| 8 | Filter student speakers to those who attended, fuzzily | L279-283 | person matching | 2C3 |
| 9 | `engagementPercent = spoke / attended × 100` | L285-295 | engagement calculation | 2C4 |
| 10 | Engagement score bands (≥80→5, ≥60→4, ≥40→3, ≥20→2) | L297-300 | engagement calculation | 2C4 |
| 11 | Checklist item 7 override (>75 Met, 50-75 Partially Met) | L302-303 | QA checklist | later |
| 12 | Silent-student list and evidence strings | L306-317 | engagement reporting | 2C4 |
| 13 | Speaker-block merge for quoting (same speaker, gap ≤ 1500 ms) | L133-166 | evidence formatting | later |
| 14 | `trainer = aiTrainer \|\| lecturerFromVtt \|\| lectureRow.trainer` | L381-382 | role inference with AI precedence | 2C3 / AI |

Only category 1 belongs in Phase 2C2. Rows 2-8 and the `lecturerFromVtt` term of row 14 are now ported in Phase 2C3 — see [phase2c3_attendance_and_roles.md](phase2c3_attendance_and_roles.md). The AI term of row 14 and rows 9-13 remain deferred.

### What Phase 2C2 actually ported

Legacy category 1, exactly:

```js
const secsBySpeaker = new Map();
for (const c of cues) {
  const sp = (c.speaker || "").trim();
  if (!sp) continue;
  const durSec = (c.endMs - c.startMs) / 1000;
  if (durSec > 0) secsBySpeaker.set(sp, (secsBySpeaker.get(sp) || 0) + durSec);
}
```

The platform computes the same quantity as `gross_spoken_ms`, in **integer
milliseconds** rather than float seconds, and stores it per document per raw
label. Two deliberate differences, neither of which changes a total:

- Legacy skips cues where `durSec > 0` is false. A zero-length cue contributes
  0 ms either way, so the sums agree; the platform still counts that cue in
  `cue_count`, which legacy did not track at all.
- Legacy trimmed the label before keying. The platform keys on the **exact raw
  label** and stores the conservative normalized form in a separate column, so
  evidence and matching aid never get confused with each other.

Legacy stopped at a `Map` in memory. The platform persists the inventory with
provenance, which is what later phases need.

## Normalization: legacy versus platform

| | Legacy `normSpeaker` (L126) | Legacy `normalize` for matching (L171) | Platform `normalize_speaker_label` |
| --- | --- | --- | --- |
| Case | `toLowerCase()` | `toLowerCase()` | `casefold()` |
| Whitespace | `trim()` | — | trim + collapse internal |
| Unicode | — | — | NFKC |
| Strips non-letters | no | **yes** (`[^a-z]` removed) | **no** |

The platform normalizer is conservative on purpose. Legacy's matching
normalizer deletes every non-letter — spaces, accents, hyphens, digits — which
is a *matching* decision that destroys information. Keeping accents and
punctuation means Phase 2C3 can still choose to be aggressive, while Phase 2C2
never silently merges two people. Two raw labels sharing a normalized form are
reported as a collision and kept apart.

## Legacy trainer diagnostic — 2026-09-04

Read-only, exact comparison only, no role assigned and no inventory adjusted:

| Measure | Result |
| --- | ---: |
| Legacy QA rows | 6 |
| Legacy trainer present as an **exact raw** speaker label | **6 / 6** |
| Legacy trainer present as an **exact normalized** speaker label | **6 / 6** |

Every legacy trainer string is already present verbatim among the canonical raw
speaker labels for its lecture. That is a strong signal for Phase 2C3: on this
date, trainer resolution would not have required fuzzy matching at all. It is
one date and six rows, so it is evidence, not a rule.

## Phase 2C3 design notes that follow from this

1. **Role inference is separable from person matching.** Legacy fused them:
   `lecturerFromVtt` (most speech) picks the trainer, then fuzzy matching picks
   students. 2C3 should keep them as two steps with their own provenance, so a
   change to matching does not silently change who the trainer is.
2. **The AI could override the trainer** (L381). Any port must decide explicitly
   whether that precedence survives, and record which source won.
3. **Engagement depends on attendance, not on the transcript.**
   `spoke / attended` counts *people*, so it cannot be computed from the
   inventory alone and must not be attempted before matching exists.
4. **Overlapping speech is irrelevant to legacy engagement** — it counts
   distinct speakers, not time. Any future time-based engagement metric will
   need the overlap-aware interval work that Phase 2C2 deliberately did not
   build.

## Carried production-cutover risks — still open

Unchanged and **not resolved** by Phase 2C2:

1. **REAL MULTIPART FIXTURE VALIDATED = NO.**
2. **Legacy Master vs One Lecture timezone divergence** has not been exercised
   on a real ambiguous multi-candidate occurrence.
