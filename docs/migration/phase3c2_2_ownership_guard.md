# Phase 3C2.2: the legacy n8n ownership guard

The legacy child workflow upserts the **same 22 session and 6 checklist
columns** the coded writer owns, keyed on the **same `session_id`**, with no
ownership awareness. This phase closes that path inside the child, which is the
single choke point every caller must cross — the Master, a manual Master rerun,
and direct invocation of the child.

**Repository implementation complete and validated. The live deployment was
attempted with a working management API and deliberately stopped without
changing anything** — the only available update operation cannot preserve the
live workflow's `settings`. See §7.

## 1. Where the guard sits

Before — four edges straight into the writes:

```
Merge1 (delivered) ─┬→ Upsert QA Session
                    └→ Upsert QA Checklist Items
Merge2 (cancelled) ─┬→ Upsert QA Session
                    └→ Upsert QA Checklist Items
```

After — one decision, both writes behind it:

```
Merge1 ─┐
        ├→ Check Coded Ownership ─→ Coded Owned? ─true→ Protected - Coded Platform Owns Session
Merge2 ─┘   (Postgres, once)                      │
                                                  └false→ Resume QA Write Rows ─┬→ Upsert QA Session
                                                                                └→ Upsert QA Checklist Items
```

Both merges are the last common point at which the final exact `session_id` is
known and stable, and they are the *only* feeders of the two upserts — so this
is the earliest safe single choke point.

## 2. Nodes added (4)

| Node | Type | Purpose |
| --- | --- | --- |
| `Check Coded Ownership` | `n8n-nodes-base.postgres` 2.6 | one parameterised `EXISTS` lookup, `executeOnce: true`, `onError: stopWorkflow` |
| `Coded Owned?` | `n8n-nodes-base.if` 2.3 | strict boolean on `coded_owned` |
| `Protected - Coded Platform Owns Session` | `n8n-nodes-base.code` 2 | no-op diagnostic on the owned branch |
| `Resume QA Write Rows` | `n8n-nodes-base.code` 2 | restores the rows the Postgres probe replaced, and re-asserts fail-closed |

Connections changed: `Merge1` and `Merge2` now target the guard instead of the
two upserts; the four guard edges are new. **No node was removed, and no
existing node's parameters, credentials or position changed** — asserted by
test against the pre-change export.

## 3. The ownership query

```sql
SELECT EXISTS (
  SELECT 1
    FROM public.lecture_qa_legacy_writes
   WHERE legacy_session_id = $1
     AND write_status = 'WRITTEN'
) AS coded_owned;
```

`$1` is bound through n8n's `queryReplacement` as `={{ $json.session_id }}` —
the primary provider transcript ID, the same value in
`qa_doctors_sessions.session_id` and `lecture_qa_legacy_writes.legacy_session_id`.
`lecture_id`, `meeting_id`, `subject` and `targetDate` are never used as
ownership keys, and the identifier is never interpolated into SQL text.

Only `WRITTEN` protects. A `ROLLED_BACK` ownership row means the coded writer
released the session, so legacy n8n may own it again — matching the Phase 3C1
rollback semantics.

Executed against live data:

| session | `coded_owned` |
| --- | --- |
| the canary (coded-owned) | **`true`** |
| Andrew (prepared, not written) | `false` |
| a historical n8n fixture | `false` |
| a nonexistent id | `false` |

`SELECT EXISTS` always returns exactly one row of type boolean.

## 4. Fail closed

Four independent layers, because treating a transient database problem as
"not owned" would turn an outage into an overwrite:

1. `onError: stopWorkflow` on the Postgres node — an unreachable database or a
   failing query stops the run instead of continuing.
2. `alwaysOutputData` is **not** set, so a failed lookup cannot manufacture an
   empty item that reads as "not owned".
3. The IF uses `typeValidation: strict` with a boolean operator, so a missing
   or non-boolean `coded_owned` errors rather than falling to the false branch.
4. `Resume QA Write Rows` re-asserts immediately before the writes:
   `if (owned !== false) throw` — `true`, `null`, `undefined` and any
   non-boolean all refuse. It also throws when no rows are available.

## 5. Behaviour

**Owned** — neither upsert executes; no QA-owned column can change. The branch
emits only:

```json
{ "qa_write_skipped": true,
  "qa_write_skip_reason": "coded_platform_owned",
  "session_id": "…" }
```

No learner names, no transcript text, no ownership metadata beyond the fact.

**Not owned** — unchanged legacy behaviour. Transcript selection, AI prompt,
model, checklist rules, engagement, LMS query, session and checklist mappings
and evidence rendering are all untouched; the only difference is that the rows
pass through one extra code node.

## 6. Item cardinality

`Build Session and Checklist Rows` emits **11 items**, each carrying the full
session payload plus its own checklist fields. `Upsert QA Session` has
`executeOnce: true` (one session row); `Upsert QA Checklist Items` fans out
across all 11.

A Postgres node *replaces* its items' payload, so the probe alone would have
destroyed all 11. `Resume QA Write Rows` therefore re-emits
`$('Merge1').all()` / `$('Merge2').all()` — exactly the rows the merge produced,
in order, with no `filter`, `slice`, `Set`, `shift` or `pop` anywhere in the
node (asserted by test). `executeOnce: true` on the probe means the lookup runs
**once per lecture, not eleven times**, while the write branch still receives
all 11 items.

## 7. Live deployment attempt (Phase 3C2.2-LIVE): **blocked by the n8n API schema**

The management API is now configured and working. The live child was fetched,
compared and backed up — but **the deployment was refused and nothing was
changed**, because the only available update operation cannot preserve the
workflow's settings.

### What succeeded

| | |
| --- | --- |
| Live workflow | `LW0lYn7XXJ9JTaYB` — "QA One Lecture - Safe Exact Recording v8" |
| Active | **true** |
| Nodes | 45 |
| `versionId` / `updatedAt` | `53295eb7-120a-4bb6-b589-e898e45c38b8` / `2026-09-05T11:48:34.922Z` |
| Backup | `docs/migration/rollback/live-QA_One_Lecture_v8-pre-phase3c2.2.json`, sha256 `0594f005cd92c918…` |
| Parity vs repo pre-change export | **all 45 nodes functionally identical, connections identical, zero drift** |

The backup contains credential *references* only (`id` + `name`); the API never
returns credential secrets.

### Why it is blocked

`PUT /api/v1/workflows/{id}` is the only update operation — there is no
`PATCH`, and `/workflows/{id}/{versionId}` is read-only. Its `workflowSettings`
schema declares `additionalProperties: false` and permits only
`saveExecutionProgress`, `saveManualExecutions`, `saveDataErrorExecution`,
`saveDataSuccessExecution`, `executionTimeout`, `errorWorkflow`, `timezone`,
`executionOrder` and `callerPolicy`. `settings` is a **required** field.

The live workflow's settings are:

```json
{"availableInMCP": true, "binaryMode": "separate", "executionOrder": "v1"}
```

The request was rejected with
`400 request/body/settings must NOT have additional properties`. Sending the
schema-valid subset would silently **drop `binaryMode` and `availableInMCP`**,
and they could not be restored afterwards through the same API.

These are not instance-wide defaults. Across the 100 workflows on this
instance the settings shape varies — `availableInMCP` is `true` on some and
`false` on others, and `binaryMode` is present on 54 and absent on 46 — so both
are deliberate per-workflow configuration. Dropping them would change
production behaviour outside the validated guard delta: `binaryMode` governs
binary handling in a workflow that downloads transcript files, and
`availableInMCP` governs external MCP exposure.

Deploying would therefore have violated "preserve everything else from the live
workflow". The run stopped instead.

### Two clean ways forward

1. **Deploy through the n8n UI** (recommended). Editing in the editor does not
   touch `settings`. Add the four nodes and repoint the six edges exactly as
   `automation/legacy_n8n/QA_One_Lecture_Safe_Exact_Recording_v8.json` defines,
   then re-run this phase's verification against the live graph.
2. **Authorise the settings change.** If an n8n owner confirms that
   `binaryMode: separate` and `availableInMCP: true` may be dropped and
   re-applied in the UI immediately afterwards, the API deployment can proceed —
   accepting a brief window in which the workflow runs on instance defaults.

### Original assessment (superseded)

Before the credential was supplied, no management API access existed at all;
only media-worker webhook URLs were present. That is no longer the blocker.

## 7a. Live deployment: original environment note

The live child is `QA One Lecture - Safe Exact Recording v8`, id
`LW0lYn7XXJ9JTaYB`, on `https://n8n.srv943390.hstgr.cloud`.

Updating it needs the n8n management API (`X-N8N-API-KEY` against
`/api/v1/workflows/{id}`). **No such credential exists anywhere in this
environment** — the only n8n values present are four media-worker *webhook*
URLs in `services/kbc-media-worker/.env`, which cannot read or write workflow
definitions. No live export could be taken, so repo-vs-live parity is
**unverified**, and nothing was deployed, created or disabled. Per the phase
contract this stops at `LIVE_GUARD_DEPLOYMENT_PENDING`.

**To deploy later:** obtain an n8n API key; `GET /api/v1/workflows/LW0lYn7XXJ9JTaYB`
and diff it against
`docs/migration/rollback/QA_One_Lecture_Safe_Exact_Recording_v8.pre-3c2.2.json`;
only if the 45 legacy nodes, both upsert mappings and the Postgres credential
still match, apply the four guard nodes and six edges; preserve the workflow's
active/inactive state, id, triggers, credentials and model configuration;
re-fetch and confirm the guard is present.

## 8. Rollback

Artifact: `docs/migration/rollback/QA_One_Lecture_Safe_Exact_Recording_v8.pre-3c2.2.json`
(sha256 `233da2bd7059d314…`, identical to the pre-change file).

Rollback is narrow — delete the four guard nodes, then repoint
`Merge1` and `Merge2` at `Upsert QA Session` and `Upsert QA Checklist Items`.
Nothing else needs reverting, because nothing else changed.

## 9. What is NOT guarded, deliberately

The Master's recording branch still updates `recording_*` on a coded-owned
session. Those columns are foreign-owned, the coded writer never sets them, and
the guarded `UPDATE` only fires while `recording_url` is blank. A test asserts
that branch contains no ownership check and writes none of the 22 mapped
columns.

## 10. Remaining cutover gates

1. **n8n coexistence** — repository guard implemented and validated; **live
   deployment pending an API credential**.
2. **Real multi-part fixture validated: NO** — Andrew prepared through 2C4;
   3A/3B not run; seam and timeline origin unreconciled.
3. **Master vs One Lecture timezone divergence** — not exercised on a real
   ambiguous multi-candidate selection.
