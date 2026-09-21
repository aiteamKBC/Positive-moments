# Phase 3C2.1: n8n Master coexistence and coded-ownership protection audit

Read-only audit. No workflow edited, no legacy row changed, no DB guard created.

## 1. The Master exists — and where Phase 3C2 went wrong

```
automation/legacy_n8n/
  QA Master Daily — Safe Exact Recording v8      48,484 bytes, 36 nodes, NO extension
  QA_One_Lecture_Safe_Exact_Recording_v8.json   126,845 bytes, 45 nodes
  README.md
```

The Phase 3C2 downstream scan used `glob.glob('automation/**/*.json')`. The
Master has **no file extension**, so it was never read, and the report's claim
that it "is not in this repository" was wrong. The earlier Phase 2B audit was
right: same path, same byte count, same 36 nodes. There is exactly one Master
in the repository and it is production-relevant — the child's
`Execute QA One Lecture` node names workflow `LW0lYn7XXJ9JTaYB` /
"QA One Lecture - Safe Exact Recording v8", matching the v8 child here.

## 2. The Master's execution contract

| | |
| --- | --- |
| Triggers | `scheduleTrigger` **and** `manualTrigger` — both feed both branches |
| Schedule | cron `0 21,23 * * *` — twice daily, 21:00 and 23:00 |
| Timezone | `Africa/Cairo`, hardcoded in `Create Date Range` |
| Date window | `targetDate` = **today only**, from `new Date()` |
| Parameterised date? | **No.** There is no date input, variable or setting |
| Checks `qa_doctors_sessions` first? | **No** |
| Checks coded ownership? | **No** |

`Create Date Range` → `Get Active Groups` → `Get Today's Calendar Events`
(`calendarView` bounded by `today` / `todayPlusOne`) → `Match Calendar to
Groups` → `Matched Lecture?` → `Execute QA One Lecture` (`mode: each`,
`waitForSubWorkflow: true`). No ownership node anywhere on that path.

The Master has a **second, independent branch** —
`Run Settings - EDIT HERE` → … → `Update Both Recording Tables` — which writes
only `recording_*` columns and is discussed in §5.

## 3. The child's write contract

`When Executed by QA Master` is an `executeWorkflowTrigger` with
`inputSource: passthrough`. `Lecture Input` accepts **`meetingId`, `module`,
`subject`, `targetDate`, `scheduledStart`, `scheduledEnd` straight from the
caller** — any date at all.

| Node | Conflict key | Columns |
| --- | --- | --- |
| `Upsert QA Session` | `session_id` | **22** |
| `Upsert QA Checklist Items` | `session_id_match` | **6** |

Those column sets are **byte-identical to the coded writer's**
`SESSION_COLUMNS` and `CHECKLIST_COLUMNS` — verified by set comparison, zero
difference either way. `session_id = baseSession.transcriptId`, the same
primary provider transcript ID the coded writer uses, so a re-run on the same
occurrence collides on the *same* key and `DO UPDATE` rewrites **every coded
field**.

The child contains **0** references to `lecture_qa_legacy_writes`,
`writer_version`, `coded_owned` or `ownership`.

Because an n8n upsert names only its mapped columns, **foreign-owned
`clips_*`, `recording_*` and `transcript_*` columns survive a legacy QA
upsert** — the damage is confined to the 22 + 6 QA fields, which is exactly
the coded writer's territory.

## 4. Canary overwrite risk

| Path | Verdict | Reason |
| --- | --- | --- |
| **A. Scheduled run, 2026-09-17+** | **OVERWRITE_NOT_POSSIBLE** | `Create Date Range` derives `targetDate` from `new Date()` in Cairo; the calendar query is bounded to that day. A run on 2026-09-17 can never emit the 2026-09-16 occurrence. |
| **B. Manual Master rerun** | **OVERWRITE_NOT_POSSIBLE (today only)** | The manual trigger feeds the *same* `Create Date Range`. There is no date input to override, so a manual rerun also processes only today. It would become possible only by **editing the code node** — a deliberate change, not a parameter. |
| **C. Historical / backfill run** | **NOT SUPPORTED by the Master** | No backfill input exists on the QA branch. (`Run Settings - EDIT HERE` has a 90-day window but belongs to the recording branch, which cannot reach `Execute QA One Lecture`.) |
| **D. Direct child invocation** | **OVERWRITE_POSSIBLE** | `Lecture Input` takes an arbitrary `targetDate` + `meetingId` from pinned or manual data. Pointed at the canary's occurrence it regenerates the identical `session_id` and overwrites all 22 + 6 fields with no ownership check. |

**Net: the canary is safe from the scheduler and from an unmodified manual
rerun; the one real exposure is a direct execution of the child workflow.** No
workflow was triggered to establish this — it is read from the graph.

## 5. A legitimate interaction that *will* occur

The Master's recording branch (`Get Target Lectures`) selects sessions where
`recording_url IS NULL`, `meeting_id` and `session_id` are present, and
`date >= CURRENT_DATE - 90 days`. **The canary matches all four.** On the next
Master run it may receive `recording_url`, `recording_item_id`,
`recording_drive_id`, `recording_filename`, `recording_link_status`,
`recording_link_updated_at`.

This is correct coexistence, not corruption: those columns are foreign-owned,
the writer never sets them, and the guarded `UPDATE` only fires when
`recording_url` is still blank. It must **not** be treated as drift — but the
coded writer's verification must keep comparing only its own 22 columns, which
it does.

## 6. Coexistence options

| | A — Master excludes owned | B — Child refuses owned | C — Disable legacy QA write branch | D — Child-side column-preserving upsert |
| --- | --- | --- | --- | --- |
| Protects scheduled run | yes | yes | yes | yes |
| Protects manual Master rerun | yes | yes | yes | yes |
| **Protects direct child call** | **no** | **yes** | yes | **yes** |
| Workflows to change | 1 | 1 | 1 | 1 |
| Blast radius | QA branch only | QA branch only | **whole legacy QA path** | QA branch only |
| Effect on current n8n production | none while no row is owned | none while no row is owned | **legacy QA stops for every lecture** | none while no row is owned |
| Rollback | revert one node | revert one node | re-enable branch | revert two nodes |
| Coexists during phased cutover | yes | yes | **no — all-or-nothing** | yes |

**Option A** puts the guard at the wrong altitude: it protects the path we
proved is already safe (§4 A/B) and leaves the only real exposure (§4 D) open.

**Option C** is the bluntest: it protects everything, but it ends legacy QA for
*all* lectures at once, which is the opposite of a phased cutover, and it is
the option to hold in reserve for the final switch.

**Option D** (add `WHERE NOT EXISTS (… lecture_qa_legacy_writes …)` to the
upsert itself) is theoretically tightest but n8n's upsert node generates its own
SQL; expressing it needs replacing the node with a raw query, duplicating 22
column mappings by hand — a large, error-prone diff in the workflow that matters
most.

**Recommended: Option B.** Insert one Postgres node plus one IF immediately
before `Upsert QA Session`:

```sql
SELECT EXISTS (
  SELECT 1 FROM public.lecture_qa_legacy_writes
   WHERE legacy_session_id = $1
     AND write_status = 'WRITTEN'
) AS coded_owned;
```

and route `coded_owned = true` to a no-op "Skipped — coded platform owns this
session" node. It sits at the single choke point every path must pass —
scheduled, manual and direct — so it satisfies the preferred safety property
without relying on "the scheduler normally runs once". It changes one workflow,
is two nodes to revert, and is inert until a row is actually owned, so today's
n8n production behaviour is unchanged for all 650 legacy sessions.

Its one gap: it protects the *session* upsert, so the checklist upsert must be
routed behind the same IF (both are downstream of the same branch point).

## 7. Database-level guard assessment — **not recommended**

A trigger is attractive as a last line of defence but is fragile here:

- **Both writers use the same tables and the same 22 columns.** A row-level
  `BEFORE UPDATE` cannot tell the coded writer's own legitimate update
  (`WOULD_UPDATE` on a re-render) from n8n's overwrite, unless it inspects
  `current_setting()` / `session_user` — which means the coded writer must set
  a session GUC on every write, adding a silent failure mode: forget the GUC
  and the platform locks itself out of its own rows.
- **Blocking all UPDATEs is far too broad.** The recording branch (§5) and
  Positive Clips both legitimately update the same rows; a blanket trigger
  would break two production workflows that are not part of this migration.
- **A column-scoped trigger is possible** (`RAISE` only when one of the 22
  mapped columns changes on an owned row) but it fires inside n8n's
  transaction, surfacing as a hard workflow error rather than a clean skip —
  turning a protected row into a nightly red run and operational noise.
- Silent suppression (`RETURN OLD`) is worse: n8n would report success while
  nothing was written.

**Conclusion: put the guard in the workflow (Option B), where it can skip
cleanly and report why.** Revisit a column-scoped trigger only as a belt-and-
braces addition *after* Option B is live, and only with the GUC question
settled.

## 8. Canary integrity

| | Before | After |
| --- | --- | --- |
| session rows | 1 | 1 |
| checklist rows | 11 | 11 |
| ownership row | `WRITTEN` | `WRITTEN` |
| writer-owned digest | `5092175cee6d2a25…` | `5092175cee6d2a25…` |
| foreign columns | 16/21 NULL, 5 schema-derived | identical |
| table counts | 651 / 7,534 / 140 / 1 | 651 / 7,534 / 140 / 1 |

All five workflow files hash-verified unchanged.

## 9. Multi-part gate: Andrew-Scheduling Professional (SP) Jan 2026

`25e85615-aa7a-5f49-bb40-078d7c7b65d0`, 2026-09-16, `downstream_ready`.

| Stage | State |
| --- | --- |
| 2B selection | `SELECTED`, `legacy_qa_v8_overlap_cluster_v1`, **2 parts** |
| parts | 1 primary, offset 0 · 2 secondary, offset **14,336,082 ms** |
| combined | 2 parts, 7,168.597 s = 119 min, parity OK (`duration_difference_ms = 0`) |
| fingerprint | `158bb1a7ad9b09a9…` |
| 2C1 parse | `PARSED_WITH_WARNINGS` — **1× `NON_MONOTONIC_CUE_START` at cue 351**, 601 cues, 0 malformed, 0 empty |
| 2C2 | 16 speakers |
| 2C3 | roster v2, 7 members, **0 ambiguous** (6 exact, 1 fuzzy, 9 non-roster) |
| 2C4 | `CALCULATED`, 7/7 = 100.00 %, score 5 |
| **3A** | **not run** |
| **3B** | **not run** |
| legacy rows / refs / ownership | 0 / 0 / 0 |

**Two findings, both located at the part boundary:**

1. **The warning is the seam.** Part 1 occupies cues 1–350
   (10,026,775 → 14,403,937 ms); part 2 begins at cue 351 (14,342,932 ms).
   That is a **61.0-second backward jump**, with **9 cues inside the overlap
   window**. Declared offset 14,336,082 ms lands precisely there. The 93
   overlapping cues (15.5 %) are *not* multi-part-specific — single-part
   lectures that day run 3–17.5 %.

2. **The cue timeline does not start near zero.** Cues span 167.11 → 286.59 min
   while `duration` reports 119 min. Every single-part lecture that day starts
   between 1.6 and 21.5 min, so a non-zero origin is normal, but 167 min is a
   different magnitude — it is part 1's own provider-relative origin inside a
   four-hour meeting window. Rendered evidence timestamps would therefore read
   `02:47:…` for the opening minutes.

Neither is proven wrong — `duration` is derived as (max end − min start) and is
internally consistent for all seven lectures — but **neither has been compared
against a real legacy multi-part QA row**, which is exactly what the carried
gate is about.

**Is Andrew safe as the second canary after ownership protection? Not yet.**
It is the *right* candidate — clean attendance, no ambiguity, no legacy row, no
downstream references — but three things must happen first: implement Option B;
run 3A and 3B for it; and reconcile the seam and timeline origin against a
legacy multi-part session before any write. Writing it today would put evidence
timestamps into production that have never been parity-checked.

## 10. Remaining cutover gates

1. **n8n coexistence** — designed (Option B), **not implemented**.
2. **Real multi-part fixture validated: NO** — candidate identified and
   audited; 3A/3B not run; seam and timeline origin unreconciled.
3. **Master vs One Lecture timezone divergence** — still not exercised on a real
   ambiguous multi-candidate selection.
