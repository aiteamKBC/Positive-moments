# Phase 3C2.3D: Perfect Lecture parity and a safe writer design

Andrew is the first coded-platform candidate that legacy QA would also have
recorded as a Perfect Lecture. This phase decides and implements what the coded
platform does about that. **No Perfect Lecture row and no QA row was written.**

## 1. The live dependency, read from the running workflows

`Get Target Lectures` in the live Master (`8yeJigbj8BCNBMkU`,
`versionId == activeVersionId == 2180528d…`) drives **FROM
`qa_doctors_sessions`** and only `LEFT JOIN`s `qa_perfect_lectures`:

```sql
FROM public.qa_doctors_sessions s
LEFT JOIN public.qa_perfect_lectures p ON p.session_id = s.session_id
WHERE NULLIF(BTRIM(s.recording_url), '') IS NULL
  AND NULLIF(BTRIM(s.meeting_id), '') IS NOT NULL
  AND NULLIF(BTRIM(s.session_id), '') IS NOT NULL
  AND s.date::date >= CURRENT_DATE - INTERVAL '90 days'
```

**An absent Perfect Lecture row does not block recording processing.** The
lecture is still found, its recording is still matched, and
`qa_doctors_sessions.recording_*` is still filled. The only thing lost is
`p.lecture_key`, which arrives NULL — and `Update Both Recording Tables` then
matches `WHERE p.lecture_key = i.lecture_key` against NULL, so the Perfect
Lecture half of the update is a silent no-op. Correct, not broken.

## 2. Active consumers of `public.qa_perfect_lectures` — there are three

Two independent sweeps over **1,344 workflows (302 active, 334 archived)**
agree:

| workflow | active | access |
| --- | --- | --- |
| `8yeJigbj8BCNBMkU` QA Master Daily v8 | **yes** | READ (LEFT JOIN) + WRITE `recording_url`, `meeting_id`, `session_id` |
| `LW0lYn7XXJ9JTaYB` QA One Lecture v8 | **yes** | WRITE (`Upsert Perfect Lecture`) — reachable only via `Execute QA One Lecture`, currently disabled |
| `Laq9uxzy5RYlnNA3` QA Perfect Lectures — Excel Sync | **yes** | READ unsynced + WRITE `excel_synced_at` |
| `aOTEH9sjIETqKjzt` Direct Organization Recording Link | no | WRITE |
| `QJ1yc5Vsh5xK8Jir` Mitigation — One Lecture + Parallel v2 | no | WRITE |
| `0RESEx8TmoIKSeiJ` My workflow 6757 | no (archived) | WRITE |

### The Excel Sync workflow is the finding that matters

It was not in any previous audit. It runs **every 15 minutes**
(`*/15 * * * *`), and its gate is:

```sql
WHERE met_count >= 11
  AND NULLIF(BTRIM(recording_url), '') IS NOT NULL
  AND excel_synced_at IS NULL
```

then it **appends the row to a Microsoft Excel workbook** and stamps
`excel_synced_at`. So a Perfect Lecture row is not an internal record: once its
`recording_url` is filled it is automatically **published outside the
database**, to a shared workbook, within fifteen minutes. That publication is
not undone by deleting the database row.

Because the coded writer never supplies `recording_url`, an Andrew row would
sit un-synced until the Master's recording branch fills it — which is exactly
the legacy sequence. It still means the real canary has an outward-facing
consequence, and that belongs in the go/no-go decision rather than in a
footnote.

**One honest limitation.** Excel Sync's saved draft (`3be73da4…`) is *not* its
running version (`e4e32d3b…`). The public n8n API has no endpoint for a
specific version — `/versions`, `/versions/{id}` and `?version=` all fail — so
the SQL above is the draft and should be confirmed in the UI before the
canary. Both the Master and the QA child have `versionId == activeVersionId`,
so those readings *are* the running definitions.

## 3. The authoritative legacy rule

From `Prepare Lecture Recording` (a code node fed by `Build Session and
Checklist Rows` — the same eleven rows that go into the QA tables):

```js
const uniqueOrders = new Set(rows.map(r => Number(r.checklist_order)).filter(Number.isFinite));
const allMet  = rows.every(r => String(r.status || '').trim() === 'Met');
const metCount = Number(first.met_count ?? 0);
const isPerfect = metCount === 11 && uniqueOrders.size === 11 && allMet
                  && first.cancelled_session !== true;
lecture_key: `${sessionDate}|${subject}`   // sessionDate = String(first.date).slice(0,10)
```

It throws outright without `meeting_id` and `session_id`. Flow:
`Update QA Session Recording → Is Perfect Lecture? (strict boolean true) →
Upsert Perfect Lecture`, the last with `onError: continueRegularOutput`.

Target `public.qa_perfect_lectures`, conflict key **`lecture_key`**, eleven
mapped columns, and a merge that is *not* uniform:

- replaced: `engagement`, `attended_count`, `met_count`
- `COALESCE(EXCLUDED.x, existing)`: `recording_url`, `meeting_id`, `session_id`,
  `module`, `trainer`
- never updated: `lecture_key`, `session_date`, `subject` — an update cannot
  move a row to a different lecture.

`recording_url` is passed as `$json.recording_url || null` *after* the
recording lookup, so legacy fills it when it has one.

## 4. Live schema and current data

15 columns; PK `id`; **UNIQUE `lecture_key`**; indexes on `session_date DESC`,
`meeting_id`, `session_id`, and a partial one on unsynced rows; **no triggers,
no foreign keys in either direction, no views**. `lecture_key`,
`session_date`, `subject`, `met_count` and `detected_at` are NOT NULL;
`detected_at` defaults to `now()`.

140 rows, 2026-04-22 → 2026-09-03.

- `lecture_key` **= `to_char(session_date,'YYYY-MM-DD')||'|'||subject` on
  140/140 rows.** No exceptions.
- `trainer`, `engagement`, `met_count`, `meeting_id`, `session_date`, `subject`
  match the joined `qa_doctors_sessions` row on **140/140**.
- `module` and `attended_count` are populated on 35 rows only, and where
  populated `module` equals `lms_module` exactly. Older rows simply predate it.
- `session_id` is populated on 140/140 and resolves to a real session on
  140/140. `meeting_id` likewise 140/140.
- `recording_url` on 118/140 — and on **45 of those it differs from the
  session's own `recording_url`**. It is genuinely foreign-owned.
- `excel_synced_at` **118/140** and `recap_url` 0/140. Those 118 are exactly
  the rows with a `recording_url`: **every perfect row that has ever received a
  recording link has already been published to Excel.**
- `detected_at` lags `session_date` by up to 90 days, 126 rows by more than a
  day: the table has been backfilled, it is not a live projection.
- **79 of 140 rows do not satisfy today's rule — and all 79 have twelve
  checklist rows.** They were correct under the twelve-item checklist of their
  day. That is the whole argument for versioning the answer.
- Legacy is already not exhaustive: **2 currently-eligible sessions have no
  Perfect Lecture row**, and nothing is broken by that.

### The collision is real

**One (date, subject) pair — 2026-02-20 — maps to two distinct legacy
sessions.** Neither is eligible today, so no wrong row exists, but the key can
collide by construction and must never become an identity.

## 5. What was built

| file | role |
| --- | --- |
| `app/qa/perfect.py` | eligibility, `PERFECT_ELIGIBILITY_VERSION`, the legacy key, and a literal transcription of the legacy expression used only by parity tests |
| `app/writer/perfect_mapping.py` | the one place `qa_perfect_lectures` is mapped, typed and digested |
| `app/db/repositories/perfect_lectures.py` | the two coded tables and the legacy target |
| `app/writer/perfect_service.py` | the planner, the write, verification, supersession and rollback |
| `app/db/migrations/015_create_perfect_lecture_tables.sql` | two additive coded tables |

### Eligibility version

`legacy_qa_v8_perfect_v1`, derived **only** from the frozen Phase 3B payload —
no model, no transcript, no recomputed status. Eligible when: exactly 11
checklist rows, orders exactly 1..11, all 11 `Met`, `met_count = 11`,
`partial_count = 0`, `not_met_count = 0`, not cancelled, and both identifiers
present.

Two deliberate differences from legacy, both **strictly narrower** — this can
refuse where legacy would accept, never the reverse:

1. legacy's `uniqueOrders.size === 11` would also accept twelve rows with one
   duplicated order; this requires exactly eleven rows with orders 1..11.
2. legacy checks only `met_count`; this also requires the two other counters to
   be zero. Under an eleven-row all-Met checklist they are zero anyway, so no
   real payload is classified differently.

A parametrised test asserts the two rules agree on every payload legacy could
have produced, and a separate test asserts the one-directional divergence is
intentional. Another test reads the executable workflow and fails if the legacy
expression or the key format ever changes.

### The coded shadow, and why two tables

`lecture_perfect_lecture_results` — canonical by `lecture_id`, unique on
`(lecture_id, eligibility_version)` — carries `is_perfect`, `reason`, the
frozen counts, `evaluation_id`, `rendered_session_id`, `source_fingerprint`,
`legacy_lecture_key`, `legacy_session_id`, `computed_at`, and a bounded
transition log in `metadata`.

`lecture_perfect_lecture_legacy_writes` — unique on
`(legacy_lecture_key, writer_version)` — is the ownership and audit record for
the legacy row.

They are separate because eligibility and legacy synchronisation are different
lifecycles. A lecture always has an answer; it only has a legacy row when the
answer is "perfect" *and* we projected it. The Operations layer must be able to
tell `NOT ELIGIBLE` from `ELIGIBLE but sync MISSING`, and `lecture_qa_legacy_writes`
cannot carry it: that table is uniquely keyed on `legacy_session_id`, while the
Perfect Lecture target is a different row keyed on `lecture_key` with its own
external writers. Overloading one audit row with two targets would make
"which target does this status describe?" unanswerable.

**No ownership column was added to `qa_perfect_lectures`.** Verified: still 15
columns, still no triggers.

### Field ownership, enforced in three places

| owner | columns |
| --- | --- |
| coded QA | `lecture_key`, `session_date`, `subject`, `module`, `trainer`, `engagement`, `attended_count`, `met_count`, `meeting_id`, `session_id` |
| recording workflows | `recording_url`, `recap_url` |
| Excel Sync / database | `excel_synced_at`, `detected_at`, `id` |

`recording_url` is forced to `None` by the mapping, refused by the repository
if anything supplies it, excluded from the coded diff, and excluded from
write verification — because a recording workflow writing it between our
INSERT and our re-read is correct behaviour, not a verification failure. The
legacy `COALESCE(EXCLUDED.recording_url, existing)` is reproduced exactly, so
passing NULL preserves whatever is there.

## 6. Transaction design — Option B

**The QA transaction commits first; the Perfect Lecture row is a separate,
idempotent, derived-output transaction.** Within that second transaction the
coded result, the legacy row and the ownership record are atomic together.

- **Legacy behaves this way.** The child upserts QA, then looks up the
  recording, then upserts the Perfect Lecture in its own statement with
  `onError: continueRegularOutput`. "QA present, Perfect absent" is a state
  production is in right now, twice.
- **The QA session is the primary and expensive artefact.** Option A would let
  a derived-output failure roll back a correct, paid-for QA write — trading a
  cheap repairable gap for an expensive one.
- **The Perfect Lecture row acquires foreign state after creation**
  (`recording_url`, `excel_synced_at`). A shared transaction implies a shared
  rollback that must never happen.
- **Recovery is deterministic either way**, because eligibility is a pure
  function of a frozen fingerprint. A missing row is repaired by re-running the
  planner; there is nothing to reconstruct and nothing to pay for.

## 7. Idempotency and the two transitions

Decisions: `PERFECT_WOULD_INSERT`, `PERFECT_WOULD_UPDATE`,
`PERFECT_WOULD_SKIP_IDENTICAL`, `PERFECT_NOT_ELIGIBLE`,
`PERFECT_NOT_ELIGIBLE_LEGACY_ROW_EXISTS`, `PERFECT_SUPERSEDED_NOT_PERFECT`,
`PERFECT_PROTECTED_EXISTING_LEGACY_ROW`, `PERFECT_BLOCKED_KEY_COLLISION`,
`PERFECT_BLOCKED_NOT_READY`, `PERFECT_BLOCKED_INVALID_PAYLOAD`.

First write → one row. Second identical write → `WOULD_SKIP_IDENTICAL`, nothing
executed. Same fingerprint → NOOP. All three proved against the real database
inside a rolled-back transaction.

**PERFECT → NOT PERFECT: supersede, never delete.** The legacy row is left
standing as history, the coded answer flips, the ownership record becomes
`SUPERSEDED_NOT_PERFECT`, and the previous answer is appended to the result's
transition log. This is the recommended policy and the implemented one. Silent
deletion is wrong here for a concrete reason: by the time the answer changes,
the row may already have been published to Excel, where deleting a database row
changes nothing.

**NOT PERFECT → PERFECT:** insert if no row exists; update only a row we own;
`PROTECTED` if a legacy-owned row exists. The transition is recorded in the
result's provenance either way.

## 8. Rollback

`rollback_owned_row` deletes only when **both** hold:

1. an ownership record exists for `(lecture_key, writer_version)` — otherwise
   `PROTECTED_EXISTING_LEGACY_ROW`, in every mode;
2. no downstream enrichment has happened — `recording_url` populated or
   `excel_synced_at` set gives `BLOCKED_DOWNSTREAM_DEPENDENCY` unless the
   caller explicitly accepts the loss. An unreadable dependency check counts as
   a dependency (fail closed).

A row that is already gone marks the audit `ROLLED_BACK` rather than erroring.

## 9. Results

| | Andrew | first canary (Ray \| PMP – June 2026) |
| --- | --- | --- |
| lecture | `25e85615…` | `8c2874d7…` |
| checklist | 11 rows, orders 1..11, all Met | 11 rows, 10 Met / 0 Partial / 1 Not Met |
| counts | 11 / 0 / 0 | 10 / 0 / 1 |
| cancelled | false | false |
| `is_perfect` | **true** (`ELIGIBLE`) | **false** (`NOT_ELIGIBLE_STATUS_NOT_ALL_MET`) |
| decision | `PERFECT_WOULD_INSERT` | `PERFECT_NOT_ELIGIBLE` |
| legacy row | none | none — matches legacy eligibility |

Andrew's key: `2026-09-16|Andrew-Scheduling Professional (SP) Jan 2026`. Not in
use, no collision.

Combined dry run (`write-legacy-qa --date 2026-09-16 --mode DRY_RUN
--lecture-id 25e85615-…`): QA `WOULD_INSERT` with 11 proposed checklist rows
(digest `83d2be26…`), Perfect `PERFECT_WOULD_INSERT` (proposed digest
`78fad6a7…`), and **0** for every written counter.

## 10. Still carried

- **Gate A** Positive Clips / media coordinate, unverified for a non-zero origin.
- **Gate B** `planner._zone` still assumes `0 <= t <= duration`.
- **Gate C** `calculate-engagement` still date-scoped.
- **Gate D** the n8n ownership guard is implemented but not deployed; QA is
  paused by a manual node disable, which is operator state, not an invariant.
- **Gate E (new)** the Excel Sync workflow's running version cannot be read
  through the public API, and it publishes outside the database every 15
  minutes.

---

## 11. Phase 3C2.3E addendum: a defect this design did not catch

Andrew's real canary wrote `qa_perfect_lectures.attended_count = NULL` where
legacy would have written **7**.

`perfect_row` reads `rendered.get("attended_count")`, but
`RenderedPayloadRepository.LOAD_RENDERED` never selected that column, so the
mapping silently produced NULL. Every unit test passed because they build the
rendered dict by hand and supply the key; the one place the two halves meet —
the real loader feeding the real mapping — was never asserted.

Legacy's value is unambiguous: `attendedCount = attendedStudents.length`, the
attendees minus the trainer, which is exactly `lecture_qa_rendered_sessions.attended_count`
= 7 for Andrew (and consistent with engagement 7/7 = 100.00 %).

Fixed in `app/db/repositories/qa_writer.py` (the loader now selects
`attended_count`) plus a regression test that compares the loader's output
against the rendered table column by column, so a mapped column the loader
does not fetch can never again reach production as a silent NULL.

**The already-written row was not repaired.** Two reasons:

1. it is outside what the canary phase authorised, and
2. it is not reachable through the normal path anyway. Idempotency keys on
   `source_fingerprint`, which has not changed — only the *mapping* changed —
   so both targets correctly report `WOULD_SKIP_IDENTICAL`. The corrected
   mapping now digests to `9d043a47…` against the written `78fad6a7…`, and
   nothing compares those two.

That second point is a genuine design gap worth closing: the planner should
compare the proposed digest against the recorded `post_write_digest` and
choose `WOULD_UPDATE` when the mapping version moved even though the source
did not. Until then the repair path is
`--mode EXPLICIT_BACKFILL --lecture-id … --allow-update-existing --confirm-write`,
which exists precisely for this.

Impact is data fidelity, not operation: the column is nullable, 105 of the 140
pre-existing rows are NULL, no active consumer filters on it (`Get Target
Lectures` only passes it through, Excel Sync does not select it).

---

## 12. Phase 3C2.3F: the repair, and closing Gate F

### The repair

`qa_perfect_lectures.attended_count` for Andrew: **NULL -> 7**, applied through
`write-legacy-qa --mode EXPLICIT_BACKFILL --lecture-id 25e85615-…
--allow-update-existing --confirm-write`. No manual SQL, no rollback, no
delete-and-recreate.

The value was derived from the persisted rendered payload
(`lecture_qa_rendered_sessions.attended_count`), not hardcoded. It matches
legacy's `attendedCount = attendedStudents.length` and is consistent with
engagement 7/7 = 100.00 %.

Result: QA `WOULD_SKIP_IDENTICAL` (untouched, digest still `83d2be26…`,
22/22 and 66/66), Perfect `PERFECT_WOULD_UPDATE` with reason
`MAPPING_OUTPUT_CHANGED`, exactly one coded-owned field diff, exactly one row
updated, **no new ownership row and no new result row** — the same
`perfect_write_id 29ce0378…` and `result_id 69a31902…` as the original write.
`id`, `recording_url`, `recap_url`, `detected_at` and `excel_synced_at` all
byte-preserved.

### Gate F: mapping-aware idempotency

The old rule treated an unchanged `source_fingerprint` as proof of identity.
It is not: the source can be frozen while the **mapping** changes, which is
precisely how the wrong `attended_count` reached production *and then survived
a second "identical" run*.

Two new things:

- `PERFECT_MAPPING_VERSION = "legacy_qa_v8_perfect_mapping_v2"` — the legacy
  mapping **contract** version, deliberately separate from
  `PERFECT_WRITER_VERSION`. Ownership is keyed on
  `(lecture_key, writer_version)`, so folding the mapping version into the
  writer version would mint a *second* ownership row for a lecture the
  platform already owns. No migration: `mapping_version` and `mapped_digest`
  live in the existing `metadata` jsonb.
- `perfect_mapped_digest(row, mapping_version=…)` — a digest over the **ten
  coded-owned columns plus the contract version**, and nothing else.
  `recording_url`, `recap_url`, `excel_synced_at`, `detected_at` and `id` are
  excluded, so a legitimate enrichment can never read as drift and can never
  provoke an endless rewrite. The whole-row `perfect_digest` still records
  them, as audit evidence.

A coded-owned target is a no-op only when **all three** hold:

| comparison | catches |
| --- | --- |
| `source_fingerprint` unchanged | a re-evaluated lecture |
| proposed mapped digest == **target's** | a changed mapping output, or any out-of-band edit of a coded-owned column |
| proposed mapped digest == **recorded** | a contract version bump whose values happen to be identical |

A recorded digest of `None` (a row written before versioning existed) lets the
target comparison decide alone, rather than forcing a rewrite of every
historical row.

Otherwise the decision is `PERFECT_WOULD_UPDATE`, carrying
`perfect_update_reason` of `MAPPING_OUTPUT_CHANGED` or `SOURCE_CHANGED`. A
mapping change **never** bypasses ownership: a legacy-owned row is still
`PROTECTED` in every mode, and `EXPLICIT_BACKFILL` still needs
`--allow-update-existing` (new: `PERFECT_BLOCKED_BACKFILL_NOT_AUTHORISED`,
matching the QA writer's posture).

One thing a test caught that the first implementation got wrong: comparing
only proposed-vs-target misses a pure contract-version bump, because both
sides are digested with the *current* version. The recorded digest is what
detects it, which is exactly why the audit must store it.

### Provenance

The ownership row's `metadata` now carries `mapping_version`, `mapped_digest`,
and a bounded `repairs` list. Andrew's entry preserves the evidence that the
first write had the old digest:

```
reason                     MAPPING_OUTPUT_CHANGED
write_mode                 EXPLICIT_BACKFILL
previous_mapping_version   null            (pre-versioning)
previous_mapped_digest     null
previous_post_write_digest 78fad6a736e45090…
new_mapping_version        legacy_qa_v8_perfect_mapping_v2
new_mapped_digest          266deb77351f1cb4…
new_post_write_digest      9d043a47d0592e6f…
```

### Proof

A second identical backfill run is `WOULD_SKIP_IDENTICAL` on both targets with
zero writes, and proposed == target == recorded. Andrew is the **only**
coded-written Perfect row, and its live `attended_count` now equals its source.
