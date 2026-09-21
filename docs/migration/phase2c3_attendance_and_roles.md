# Phase 2C3: attendance resolution and deterministic speaker roles

Two deliberately separate layers on top of the Phase 2C2 speaker inventory:

```
lecture_transcript_speakers
  ├── person resolution  ->  lecture_transcript_speaker_identities
  └── role inference     ->  lecture_transcript_speaker_roles
```

"Speaker X matches learner 123" and "Speaker X is the trainer" are different
claims with different evidence, so they are stored separately and versioned
separately. Changing the matching algorithm cannot rewrite stored trainer
evidence, and changing the trainer rule cannot rewrite person resolution.

No engagement metric is computed. That is Phase 2C4.

## 1. Legacy source inspected

Authoritative export: `automation/legacy_n8n/QA_One_Lecture_Safe_Exact_Recording_v8.json`.

| Legacy element | Export location |
| --- | --- |
| `Aggregate Attendees` — bot filter, blank-name drop, `name\|email` dedup | node at L263 |
| `Attendance Subflow` — passes `meetingId`, `module`, `targetDate` | node at L337 |
| `normalize`, `toTokens`, `levenshtein`, `isSimilar` | `Build Session and Checklist Rows`, "Fuzzy matcher — v7" |
| `lecturerFromVtt` = top speaker by seconds | same node, L256-257 |
| `trainer = aiTrainer \|\| lecturerFromVtt \|\| lectureRow.trainer` | same node, L381-382 |

**Gap recorded honestly.** The subflow `Attendance (DB-based)`
(`workflowId: qe3vE6QflTZU7SMz`) is referenced but **not present in the
export**, so its SQL text could not be read. The only authoritative statement
of its behaviour is the `Aggregate Attendees` header comment:

> queries `public.kbc_attendance` filtered by module + date + Attendance=1

That contract — and nothing broader — is what was ported. No roster rule was
invented to fill the gap.

One schema detail follows from the same gap: `kbc_attendance` has a `FullName`
column while `Aggregate Attendees` reads `j.Name`, so the unexported subflow
must alias it. The platform reads `FullName` directly.

## 2. Attendance query contract

```sql
SELECT a."ID", a."FullName", a."Email", a."Attendance", a.module
  FROM public.kbc_attendance a
 WHERE a.date = :session_date
   AND btrim(regexp_replace(lower(a.module), '\s+', ' ', 'g')) = :module_normalized
   AND a."Attendance" = 1
```

- date comes from `lecture_sessions.session_date` (the Cairo business date
  established in Phase 1), never from a calendar lookup;
- module comes from `lecture_sessions.module`, normalized with the platform's
  existing `normalize_group`, matched **exactly** after normalization;
- `Attendance = 1` only.

No calendar rediscovery, no Aptem query, and no other table contributes a
single roster row. `kbc_users_data` and `aptem_auto_extracting` are not read at
all in this phase.

## 3. Bot / notetaker exclusion — ported verbatim

Legacy `isBot(name, email)`: lowercase both fields, and exclude the row if
either contains any of

`notetaker`, `otter.ai`, `otter ai`, `read.ai`, `read ai`, `fireflies`,
`fathom`, `tldv`, `meeting bot`, `meeting notes`, `transcript bot`,
`recording bot`, `zoom ai`, `copilot`, `teamsmaestro`, `teams maestro`

Sixteen patterns, unchanged. None added, none removed. Versioned as
`legacy_aggregate_attendees_v6`.

## 4. Deduplication — ported, with one privacy deviation

Legacy key: `name.toLowerCase().trim() + "|" + email.toLowerCase().trim()`,
first occurrence wins, missing email becomes `""`.

The platform keeps those semantics exactly but stores the email half as its
SHA-256:

```
dedup_key = lower(trim(name)) + "|" + sha256(casefold(trim(email)))
```

Equality — the only property dedup uses — is identical, and the key is
persisted on every snapshot member, so keeping the address in clear would put
contact data into a derived table for no functional gain. Consequences are
unchanged: same name with different emails stays two people; same name with no
email merges.

## 5. `isSimilar` — ported exactly

```
normalize(n)  = lowercase, delete every character outside a-z
toTokens(s)   = lowercase, split whitespace, strip non-letters, keep len >= 2
method 1      = 1 - levenshtein(na, nb) / max(len) >= 0.8
method 2      = every token of the shorter name matches some token of the
                longer one: exact, or prefix with BOTH tokens >= 3 chars
isSimilar     = method 1 OR method 2
```

Not simplified and not tuned. It is a compatibility function, kept in
`app/attendance/legacy_matching.py` and used only as the third matching stage.

Its inherited asymmetry (argument order can matter when token counts tie) is
neutralised by always calling `is_similar(speaker_label, member_name)`.

## 6. Matching order and the safety guard

| Stage | Status | Score |
| --- | --- | --- |
| 1 | `EXACT_MATCH` — raw label equals raw member name | 1.0 |
| 2 | `NORMALIZED_EXACT_MATCH` — conservative NFKC/collapse/casefold equality | 0.95 |
| 3 | `LEGACY_FUZZY_MATCH` — `isSimilar` | 0.8 |
| — | `AMBIGUOUS` — more than one candidate at the deciding stage | null |
| — | `NO_MATCH` — no candidate | null |
| — | `NOT_APPLICABLE` — the roster is empty | null |

Fuzzy matching never runs once an exact stage has produced a unique answer.

**The guard is an intentional divergence.** Legacy only ever asked a boolean
(`attendedStudents.some(isSimilar)`) — it never identified *which* attendee
matched, so it could not detect that two were plausible. The platform records a
person reference, so it must refuse to guess: two or more candidates yield
`AMBIGUOUS` with `matched_member_id NULL`, enforced by a table CHECK. Candidate
order is sorted before reporting, never used to pick.

## 7. Role inference

`ROLE_ALGORITHM_VERSION = vtt_top_speaker_v1`,
`deterministic_trainer_source = VTT_TOP_SPEAKER`.

- **TRAINER_CANDIDATE** — the speaker with the greatest Phase 2C2
  `gross_spoken_ms`. Speech time is never recomputed from cues. Ties break on
  `first_cue_index` (legacy's stable sort over a Map built by walking the cues
  in order meant the earlier speaker won) and then on the raw label, so no
  outcome can depend on database row order.
- **LEARNER** — a non-trainer speaker that person resolution safely tied to a
  roster member. Being "not the trainer" is never sufficient.
- **OTHER_OR_UNRESOLVED** — everything else, including `AMBIGUOUS`.

**The AI trainer override is NOT implemented.** Legacy's final precedence was
`aiTrainer || lecturerFromVtt || lectureRow.trainer`; only the middle term
exists here. A future AI layer records a separate decision and must never
rewrite this row.

**Trainer/attendance intersection.** If the top speaker also matches a roster
member, both facts are preserved: the role stays `TRAINER_CANDIDATE` and the
resolution row keeps its match. `trainer_also_matched_attendance` reports it as
a diagnostic.

## 8. Snapshot provenance and fingerprint

`kbc_attendance` is live and externally owned, so the roster actually used is
frozen into `lecture_attendance_snapshots` keyed by a fingerprint of its own
contents.

Fingerprint ingredients, newline-joined then SHA-256:

1. `version:<attendance_resolution_version>`
2. `date:<lecture session_date, ISO>`
3. `module:<normalized module>`
4. one **sorted** line per effective member:
   `external_person_id | normalized name | email sha256`

Database row order is excluded by construction. Any change to a member's id,
name or email changes a line and therefore the snapshot id, which creates a new
snapshot instead of reinterpreting an old resolution.

`snapshot_id = uuid5(lecture, resolution_version, fingerprint)`,
`resolution_id = uuid5(speaker, snapshot, resolver_version)`,
`role_id = uuid5(speaker, role_version, resolver_version, snapshot)`.

The role key includes the resolver version because the LEARNER rule consumes
person resolution; without it a new matching algorithm would silently rewrite
role evidence under an unchanged role-algorithm version.

## 9. Real validation — 2026-09-04

68 speakers across 7 lectures, 78 effective attendance members.

| Lecture | Speakers | Eff. attendance | Bots | Dedup | Exact | Norm | Fuzzy | Ambig | Unmatched | Learners | Other |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| AI in Project Control 2026 | 7 | 4 | 0 | 0 | 3 | 0 | 0 | 0 | 4 | 3 | 3 |
| Femi-Commercial Intelligence-Oct 25 | 10 | 14 | 0 | 0 | 1 | 0 | 4 | 0 | 5 | 5 | 4 |
| G2-Juliane -Impact and Planning June 2026 | 9 | 1 | 0 | 0 | 0 | 0 | 1 | 0 | 8 | 1 | 7 |
| Project Planning & Control (PPC) \| Andrew | 8 | 6 | 0 | 0 | 3 | 0 | 2 | 0 | 3 | 5 | 2 |
| Ray-Project Management Office (PMO) | 8 | 12 | 0 | 0 | 0 | 1 | 3 | 0 | 4 | 4 | 3 |
| G3 - Femi - Customer Journey Optimisation | 9 | 16 | 0 | 0 | 7 | 0 | 0 | 0 | 2 | 7 | 1 |
| Ray-MSP Jan 2026 | 17 | 25 | 0 | 0 | 7 | 1 | 3 | 0 | 6 | 11 | 5 |
| **Total** | **68** | **78** | **0** | **0** | **21** | **2** | **13** | **0** | **32** | **36** | **25** |

**Legacy trainer parity: 6 / 6 exact raw, 6 / 6 exact normalized.** The
deterministic top-speaker rule alone reproduces every stored
`qa_doctors_sessions.trainer` on this date — no AI override and no lecture-row
fallback is needed to explain any of the six.

**Trainer/attendance intersection: none found.** All seven trainer candidates
resolve to `NO_MATCH` against their roster, which is consistent with
`kbc_attendance` holding learners rather than trainers. The intersection case
is implemented and unit-tested; it simply did not occur on this date.

**Safe-vs-legacy matching differences: 0.** For all 68 speakers the legacy
boolean and the safe resolver agree. The ambiguity guard changed no outcome
here — it never had to fire.

## 10. Still open — carried cutover risks

Unchanged and **not resolved** by Phase 2C3:

1. **REAL MULTIPART FIXTURE VALIDATED = NO.**
2. **Legacy Master vs One Lecture timezone divergence** has not been exercised
   on a real ambiguous multi-candidate occurrence.

## 11. Phase 2C4 entry point

The deterministic inputs engagement needs are now persisted and versioned:
the frozen roster (`effective_member_count`), the resolved learner speakers,
and the trainer candidate.

Legacy engagement was `spoke / attended × 100`, where `attended` **excluded
attendees similar to the lecturer** (L279-283). That exclusion is an engagement
decision, not a roster decision, so Phase 2C3 deliberately did not remove those
members from the snapshot; 2C4 must apply it at calculation time and record it
in its own version. 2C4 should also decide explicitly what an `AMBIGUOUS`
speaker means for a participation rate — legacy could not represent one.
