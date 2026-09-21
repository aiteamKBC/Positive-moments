# Phase 2C4: deterministic learner engagement

```
lecture_attendance_snapshots / _members          (Phase 2C3, frozen roster)
lecture_transcript_speaker_identities            (Phase 2C3, person resolution)
lecture_transcript_speaker_roles                 (Phase 2C3, deterministic roles)
        │
        ▼
lecture_engagement_metrics + lecture_engagement_participants
        │
        ▼
Phase 3 QA (not started)
```

The normal calculation reads **only** persisted Phase 2C3 evidence. It never
reads `public.kbc_attendance`, never rematches speakers, and never writes a QA
table. `learner_engagement_status` is a deterministic recommendation for
Phase 3, not a checklist write.

Algorithm version: **`legacy_qa_v8_engagement_v1`** — the engagement block of
the legacy `QA One Lecture — Safe Exact Recording v8` workflow, ported with the
safety changes listed below. Trainer exclusion is versioned separately as
`trainer_exclusion_legacy_is_similar_v7_safe_v1`.

## 1. Legacy source

`automation/legacy_n8n/QA_One_Lecture_Safe_Exact_Recording_v8.json`, node
`Build Session and Checklist Rows`, code lines 273-317:

```js
const attendedStudents = attendanceRows
  .map(r => (r.Name || r.name || "").trim()).filter(Boolean)
  .filter(name => !lecturerFromVtt || !isSimilar(name, lecturerFromVtt));
const attendedCount = attendedStudents.length;
const studentSpeakers = rawStudentSpeakers.filter(name =>
  attendedStudents.some(att => isSimilar(name, att)));
const spokeCount = studentSpeakers.length;
if (attendedCount > 0) {
  engagementPercent = Number(((spokeCount / attendedCount) * 100).toFixed(2));
  // >=80 -> 5, >=60 -> 4, >=40 -> 3, >=20 -> 2, else 1
  // >75 -> "Met", >=50 -> "Partially Met", else "Not Met"
  const silentStudents = attendedStudents.filter(att =>
    !rawStudentSpeakers.some(sp => isSimilar(att, sp)));
}
```

When `attendedCount` is 0: `engagementPercent = 0`, `engagement_score = 1`, and
`item7OverrideStatus` stays `null`, so the AI's own Item 7 answer is kept.

## 2. Rules

**Trainer exclusion.** Legacy `isSimilar(memberName, lecturerFromVtt)`, same
argument order, against every effective snapshot member. This is the only
comparison Phase 2C4 makes itself, because Phase 2C3 deliberately kept the full
roster.

| Matches | Outcome |
| --- | --- |
| 0 | `TRAINER_NOT_IN_ATTENDANCE`, nobody excluded |
| 1 | `TRAINER_EXCLUDED`, that member leaves the denominator |
| more than 1 | `TRAINER_ATTENDANCE_AMBIGUOUS`: **no percentage, no score, no Item 7**. The candidate members are `UNDETERMINED`. The legacy result (which removed every match) is kept only as `metadata.legacy_compatible_attended_count`. |
| no trainer candidate | `NO_TRAINER_CANDIDATE`, nobody excluded (legacy `!lecturerFromVtt`) |

**attended_count.** The count of effective Phase 2C3 snapshot members, after
blank-name removal, bot removal and `name|email` dedup, **minus** the trainer
exclusion. Raw rows, present rows, speaker count and LMS group size are never
used.

**spoke_count.** The number of **unique** denominator members with at least one
speaker where:

- the role is `LEARNER`;
- the resolution is `EXACT_MATCH`, `NORMALIZED_EXACT_MATCH` or `LEGACY_FUZZY_MATCH`;
- `matched_member_id` belongs to this snapshot and was not trainer-excluded.

It never counts the trainer, `OTHER_OR_UNRESOLVED`, `AMBIGUOUS` or `NO_MATCH`
speakers, or a second alias of the same learner.

**Silent learners.** Denominator members with no such speaker. They are stored
as participant rows with status `SILENT`, holding member references only — no
names are copied.

**Percentage.** `Number(((spoke / attended) * 100).toFixed(2))`. The division and
multiplication are the same IEEE-754 operations in Python. `toFixed(2)` is
reproduced by rounding the **exact** double half-up (`Decimal(x)` with
`ROUND_HALF_UP`). Python's `round()` is not equivalent: `round(3.125, 2)` is
3.12, while JS gives 3.13. The pinned Node.js reference cases include
1/32 → 3.13, 5/32 → 15.63, 2.675 → 2.67 and 79.995 → 80.

**Bands and Item 7.** Both are evaluated on the **rounded** value, as legacy did.
Score bands: ≥80 → 5, ≥60 → 4, ≥40 → 3, ≥20 → 2, otherwise 1. Item 7: >75 →
`Met`, ≥50 → `Partially Met`, otherwise `Not Met`. Exactly 75 is
`Partially Met` (score 4).

**Zero attendees.** Stored as 0.00 and score 1 for parity, with
`calculation_status = NO_ATTENDED_LEARNERS`, `learner_engagement_status = NULL`
and `item7_override_applied = false`. "Nobody attended" therefore never looks
like "everybody was silent", which is `CALCULATED` at 0% and `Not Met`.

## 3. Ambiguous speakers

An `AMBIGUOUS` resolution never counts as speech. The result records
`ambiguous_speaker_count` plus an upper bound: the spoke count if each
ambiguous speaker were one more **silent member it was a candidate for**. If
that bound would change the score or Item 7, the status is
`REVIEW_AMBIGUITY_MAY_CHANGE_RESULT`; otherwise it is
`CALCULATED_WITH_AMBIGUITY`. The stored numbers themselves never guess.

## 4. Deliberate divergences from legacy

| Legacy | Platform | Why |
| --- | --- | --- |
| `spokeCount` counts speaker labels | counts unique attendance members | two labels for one learner inflated legacy, which could even exceed 100% |
| who spoke = `attendedStudents.some(isSimilar)` recomputed at run time | persisted Phase 2C3 resolution | reproducibility; exact-first matching; ambiguity guard |
| every roster match for the trainer is removed | more than one match → no result | a denominator nobody can defend is not reported |
| silent = attendees not similar to **any** non-trainer label | denominator members with no safely resolved learner | an unmatched label cannot silently "cover" an attendee |
| Item 7 value applied to the checklist | recommendation only | Phase 3 owns QA writes |

None of these changed a result on 2026-09-04: there were no duplicate aliases, no
ambiguous speakers and no trainer on any roster, and silent counts equal
legacy wherever the rosters agree.

## 5. Provenance

`source_fingerprint` is a SHA-256 over sorted, newline-joined lines:

- the algorithm version;
- the snapshot id plus its roster fingerprint;
- the resolver version and the role version;
- the document id;
- one line per member id;
- one line per speaker: `id|role|rank|resolution status|matched member`.

`engagement_id = uuid5(document, snapshot, resolver, role version, engagement
version, fingerprint)`. An unchanged rerun lands on the same row. Changing the
snapshot, resolver, role algorithm, engagement algorithm or any underlying
decision adds a new row, and the old one is kept.

When a lecture has several snapshots, all of them are calculated (idempotent
history). `is_current_snapshot` marks the newest, with ties broken by snapshot
id.

## 6. Real results — 2026-09-04

Trainer exclusions: **0** on every lecture (`TRAINER_NOT_IN_ATTENDANCE`).
Ambiguous speakers: **0**. Status: **CALCULATED** for all seven.

| Lecture | Roster | Attended | Learner spk | Spoke | Silent | Unresolved | % | Score | Item 7 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| AI in Project Control 2026 *(new only)* | 4 | 4 | 3 | 3 | 1 | 3 | 75.00 | 4 | Partially Met |
| Femi-Commercial Intelligence-Oct 25 | 14 | 14 | 5 | 5 | 9 | 4 | 35.71 | 2 | Not Met |
| G2-Juliane -Impact and Planning June 2026 | 1 | 1 | 1 | 1 | 0 | 7 | 100.00 | 5 | Met |
| Project Planning & Control (PPC) \| Andrew | 6 | 6 | 5 | 5 | 1 | 2 | 83.33 | 5 | Met |
| Ray-Project Management Office (PMO) | 12 | 12 | 4 | 4 | 8 | 3 | 33.33 | 2 | Not Met |
| G3 - Femi - Customer Journey Optimisation | 16 | 16 | 7 | 7 | 9 | 1 | 43.75 | 3 | Not Met |
| Ray-MSP Jan 2026 | 25 | 25 | 11 | 11 | 14 | 5 | 44.00 | 3 | Not Met |

## 7. Legacy parity and the roster-drift finding

| Measure | Result |
| --- | ---: |
| spoke count (from legacy's own Item 7 evidence) | **6 / 6** |
| algorithm replayed on legacy's counts (% + score + Item 7) | **6 / 6** |
| attended count | 3 / 6 |
| Engagement % | 3 / 6 |
| engagement_score | 5 / 6 |
| Item 7 | 4 / 6 |

| Lecture | New | Legacy | Cause |
| --- | --- | --- | --- |
| G3 Femi | 7/16 = 43.75, 3, Not Met | 7/12 = 58.33, 3, Partially Met | attended denominator |
| Ray-MSP | 11/25 = 44.00, 3, Not Met | 11/18 = 61.11, 4, Partially Met | attended denominator |
| Ray-PMO | 4/12 = 33.33, 2, Not Met | 4/11 = 36.36, 2, Not Met | attended denominator |

**Cause, proven.** The extra members are exactly the live `kbc_attendance` rows
for that date and module whose `attendance_status = 'makeup'`: 4 for G3, 7 for
MSP and 1 for PMO. All twelve:

- have live `activity = 0`;
- were marked resolved/makeup on **09-09, 09-10 or 09-11** — 5-7 days **after**
  the legacy QA run (09-04 around 20:02-20:07 GMT);
- did not speak.

The Phase 2C3 snapshot was taken on 09-16, so it froze a roster that the
follow-up process had changed since the legacy run. With those twelve members
left out, the persisted evidence reproduces legacy **6/6 on Engagement, score,
Item 7 and attended count**.

The read-only diagnostic that shows this is
`python -m app.cli.audit_engagement_roster_drift --date 2026-09-04`. It is a
separate tool, runs in a PostgreSQL read-only transaction, and prints counts
and dates only. It is **not** part of the normal calculation, which never
reads `kbc_attendance`.

Nothing was adjusted to force parity, and the stored 2C4 results are the
honest outcome of the 2C3 contract as it stands.

**RESOLVED — see [the makeup roster rule](#7a-resolved-the-makeup-roster-rule-v2).**
The approved KBC rule is that a learner marked makeup after the lecture is not
an attendee of the original lecture for engagement. It is implemented as
roster version `attendance_roster_v2_exclude_makeup`, and under it all six
legacy lectures reach full parity. The v1 results in this section are retained
as the historical record of the old rule.

**Identifier note.** `kbc_attendance."ID"` is a per-learner id (518 distinct
values across 17k rows); the row key is `key`. The Phase 2C3
`external_person_id` therefore holds a genuine learner id; the roster code's
source field was renamed from `source_row_id` to `learner_id` to match. No
2026-09-04 snapshot contains the same learner id twice, and a roster that did
would report it as `duplicate_learner_id_count` rather than merge silently.

## 7a. RESOLVED: the makeup roster rule (v2)

**Business rule.** Engagement measures participation in the ORIGINAL lecture.
A later makeup/recovery status must not retroactively change the denominator
of a historical lecture.

**Marker.** `public.kbc_attendance.attendance_status = 'makeup'`, an exact
lowercase value. The source table's own CHECK constraint restricts the column
to `('original', 'makeup', 'absent')` or NULL, so no case or spacing variant
can exist. The column is sparsely populated: 241 makeup rows table-wide, no
`original` or `absent` rows, everything else NULL.

**Conservative scope.** Only that field is consulted. Zero activity, a later
timestamp, a repeated learner id, silence, and name similarity are all
explicitly NOT evidence of makeup — 4,603 rows have `activity = 0` with no
makeup flag and are all retained.

**Versioning.** The rule lives in `app/attendance/roster.py` as a version
registry:

| Version | Rule |
| --- | --- |
| `attendance_roster_legacy_v1` | ignores `attendance_status` entirely (historical) |
| `attendance_roster_v2_exclude_makeup` | drops rows marked makeup before the blank-name, bot and dedup steps |

The version is an ingredient of the roster fingerprint and of the snapshot id,
so the same source data under a different rule yields different provenance.
v1 remains reproducible: re-running it reuses the stored v1 snapshots and
creates nothing.

**Effect on 2026-09-04** (recomputed from live evidence, not hardcoded):

| Lecture | v1 roster | makeup excluded | v2 roster |
| --- | ---: | ---: | ---: |
| AI in Project Control 2026 | 4 | 0 | 4 |
| Femi-Commercial Intelligence-Oct 25 | 14 | 0 | 14 |
| G2-Juliane -Impact and Planning June 2026 | 1 | 0 | 1 |
| Project Planning & Control (PPC) \| Andrew | 6 | 0 | 6 |
| Ray-Project Management Office (PMO) | 12 | 1 | 11 |
| G3 - Femi - Customer Journey Optimisation | 16 | 4 | 12 |
| Ray-MSP Jan 2026 | 25 | 7 | 18 |

No speaker decision changed: identity statuses, matched people, trainer
candidates and learner roles are identical under v1 and v2, because no makeup
learner was a matched speaker on this date.

**Engagement under v2, and legacy parity:**

| Lecture | Attended | Spoke | Silent | % | Score | Item 7 | Legacy match |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| AI in Project Control 2026 | 4 | 3 | 1 | 75.00 | 4 | Partially Met | no legacy row |
| Femi-Commercial | 14 | 5 | 9 | 35.71 | 2 | Not Met | all |
| G2-Juliane | 1 | 1 | 0 | 100.00 | 5 | Met | all |
| PPC \| Andrew | 6 | 5 | 1 | 83.33 | 5 | Met | all |
| Ray-PMO | 11 | 4 | 7 | 36.36 | 2 | Not Met | all |
| G3 Femi | 12 | 7 | 5 | 58.33 | 3 | Partially Met | all |
| Ray-MSP | 18 | 11 | 7 | 61.11 | 4 | Partially Met | all |

**6 / 6** on attended count, spoke count, Engagement percentage,
engagement_score and Item 7 — and 6/6 on silent counts too. AI in Project
Control is unchanged by the rule (no makeup rows) and still has no QA row.

**Both rules remain on record**, so the difference between them is auditable:

| | v1 | v2 |
| --- | ---: | ---: |
| snapshots | 7 | 7 |
| snapshot members | 78 | 66 |
| speaker identities | 68 | 68 |
| speaker roles | 68 | 68 |
| engagement results | 7 | 7 |
| participants | 78 | 66 |

## 8. Still open — carried cutover risks

1. **REAL MULTIPART FIXTURE VALIDATED = NO.**
2. **Legacy Master vs One Lecture timezone divergence** has not been exercised
   on a real ambiguous multi-candidate occurrence.
3. ~~post-lecture `makeup` roster drift~~ — **RESOLVED** by roster version
   `attendance_roster_v2_exclude_makeup` (§7a).

## 9. Phase 3 entry point

For each lecture, read the `is_current_snapshot` row of
`lecture_engagement_metrics` (latest snapshot) under the pinned versions.

- Use `learner_engagement_status` as the deterministic Item 7 value when
  `item7_override_applied` is true.
- Keep the AI Item 7 answer when it is false (`NO_ATTENDED_LEARNERS`).
- Route `REVIEW_AMBIGUITY_MAY_CHANGE_RESULT` and `TRAINER_ATTENDANCE_AMBIGUOUS`
  to review.
- Build the Item 7 evidence text from `lecture_engagement_participants` at
  render time, so names are joined in only where the QA output needs them.
