# Positive Clips — Production Orchestration (Phase 1)

Event-driven producer + hourly reconciliation, with a priority queue so new
lectures never wait behind the historical backfill.

```
V5 analysis finishes a lecture
        |
        |  saves positive_clips to qa_doctors_sessions
        v
Producer sub-workflow (session_id, mode)
        |   1. persist clips_media_origin = live | history
        |   2. queue jobs, priority derived from that origin (live=100, history=10)
        v
public.qa_media_jobs (status=pending)
        |
        |  local FFmpeg worker, concurrency 1, claims highest priority first
        v
SharePoint  ->  COMPLETE webhook  ->  qa_positive_clip_assets

Hourly reconciliation = safety net for anything the producer missed.
```

**V5 never waits for media.** The producer only inserts rows; if the worker is
offline the jobs simply sit `pending`. If the producer call itself fails after
V5 saved its clips, the hourly reconciliation creates the missing jobs later.

## Identity rules (unchanged, do not alter)

| Thing | Rule |
|---|---|
| `clip_key` | `session_id:start_cue:end_cue` |
| `job_key` | `positive:<md5(session_id)[:8]>:<start_cue>:<end_cue>` |
| filename | `YYYYMMDD_<safe-subject>_<md5(session_id)[:8]>_clip-NN_cue-X-Y.mp4` |
| padding | 60s before, 60s after |
| job | `job_type=positive_clip`, `cut_mode=precise`, `part_number=NULL` |

Live and history modes differ **only** in the persisted `clips_media_origin`
and the `priority` it implies (plus the matching `metadata.queue_origin` stamped
on each job). There is one identity system, not two.

## Priority

`qa_media_jobs.priority smallint NOT NULL DEFAULT 10` (migration 050).

* `100` — new / live lecture
* `10` — historical backfill

Claim order: `priority DESC, created_at ASC, job_id ASC`, served by
`qa_media_jobs_claim_idx (status, not_before, priority DESC, created_at, job_id)
WHERE status IN ('pending','failed')`, which replaced the old
`qa_media_jobs_pending_idx`.

## Explicit origin (replaces the date rule)

Priority comes from an explicit, persisted field on the lecture:

```sql
qa_doctors_sessions.clips_media_origin text NULL
    CHECK (clips_media_origin IS NULL OR clips_media_origin IN ('history','live'))
```

| origin | priority |
|---|---|
| `live` | 100 |
| `history` | 10 |

**Lecture date never decides this.** A lecture delivered 3 days ago that is being
analysed as part of the historical backfill stays `history` / priority 10. Only
genuinely new production processing is `live` / 100.

The origin is written **before** any media job exists, which is why it lives on
the lecture and not only in `qa_media_jobs.metadata`. If the producer crashes
between "V5 result saved" and "media job created", the lecture still carries its
origin and hourly reconciliation recreates the jobs at the correct priority.

`metadata.queue_origin` is still stamped on each job, copied from the lecture's
origin, so a job always records the origin it was created under.

### Conflicts are rejected, never silently converted

`064_assert_session_media_origin.sql` returns one outcome per session:

| outcome | meaning |
|---|---|
| `set` | origin was NULL, now stamped |
| `unchanged` | already equals the requested mode (idempotent re-run) |
| `conflict` | stored origin differs and no override was granted - **nothing changed** |
| `overridden` | stored origin differs and override *was* deliberately granted |
| `not_found` | session_id does not exist |

The producer stops on `conflict` / `not_found`. `history` is never silently
promoted to `live`, nor the reverse. `allow_override` is an explicit
administrative action and is reported as `overridden`.

### Missing origin is reported, never guessed

A V5-final lecture with clips still to queue but no origin is a configuration
mistake. Reconciliation **skips** it rather than inventing a priority. It
surfaces as:

* `pipeline_status = 'origin_missing'` and `origin_missing = true` in the status view
* `monitoring/K_unresolved_origin.sql` and `producer/065_unresolved_origin.sql`
* `unresolved_origin_lectures` in the reconciliation summary

After migration 051 this should be zero.

### LIVE_WINDOW_DAYS

Retained **only** as a human-readable diagnostic hint on the unresolved-origin
report (`date_based_hint`, labelled "suggestion only"). It sets no priority
anywhere and is not consulted by the producer, the reconciler, or the backfill
preview.

## Idempotency and retries

A clip is skipped when **either** a completed asset exists for its `clip_key`,
**or** a job for that `clip_key` is `pending` / `processing` / `completed`.
Backstopped by `ON CONFLICT (job_key) DO NOTHING`.

Failed jobs are **never duplicated**. `062_requeue_retryable_failed.sql` updates
the existing row in place:

* preserves `job_id`, `job_key`, `clip_key`, `attempt_count`
* clears only `locked_by`, `locked_at`, `started_at`
* sets `status = 'pending'` with backoff `not_before = NOW() + LEAST(attempt_count,6) * 5 minutes`
* requires `attempt_count < max_attempts`
* skips any clip that already has a completed asset

`attempt_count >= max_attempts` is **never** auto-requeued — it surfaces as
`jobs_exhausted` in the status view and in monitoring query `G`.

## Backpressure

Historical V5 batch scheduling should pause while the history lane is deep,
without ever throttling live lectures. Query `monitoring/J_backpressure_gate.sql`
returns `ok_to_schedule`:

> pause history batches when `history_in_queue > 200` (pending + processing,
> priority < 100); live lectures are unaffected because they use a separate
> priority lane.

Chosen from measured throughput: 4 completed jobs averaged **85.1 s** each
(~0.6 s of processing per second of clip). At 200 queued history jobs the lane
drains in roughly 4.7 hours at concurrency 1, so a live lecture arriving mid-drain
waits only for the single in-flight job, not the backlog. This is decision logic
for the scheduler — it is not enforced in the database.

## Files

```
sql/migrations/050_add_queue_priority.sql          applied (priority column)
sql/migrations/051_add_clips_media_origin.sql      applied (explicit origin)
sql/040_claim_next_media_job_priority.n8n.sql      needs manual n8n paste
sql/producer/060_positive_clip_candidates.sql      candidate CTE, exposes media_origin
sql/producer/061_insert_positive_clip_jobs.sql     single insert, origin-driven priority
sql/producer/062_requeue_retryable_failed.sql      in-place failed-row requeue
sql/producer/064_assert_session_media_origin.sql   persist origin, reject conflicts
sql/producer/065_unresolved_origin.sql             lectures with no origin
sql/views/070_pipeline_status_view.sql             applied
sql/monitoring/A..L                                operational queries
sql/backfill/080_next_v5_batch_preview.sql         read-only next-N selector (always history)
sql/backfill/081_v5_backlog_summary.sql            read-only backlog summary
n8n/kbc-positive-clips-producer-v3.json            import, inactive
n8n/kbc-positive-clips-reconciliation.json         import, inactive
run_producer.py                                    controlled local runner
```

`sql/010_*` and `sql/011_*` are superseded by `sql/producer/060_*` and `061_*`.
The reconciler no longer has its own insert: producer and reconciliation share
`061`, because priority now comes from the lecture's origin rather than a
caller-supplied scalar. There is exactly one insert path and one priority rule.

## Running the producer locally

```
run_producer.py --preview --one-per-lecture --limit 3
run_producer.py --insert --mode live    --session-id '<session_id>'
run_producer.py --insert --mode history --clip-key '<clip_key>'
run_producer.py --insert --mode live --session-id '<id>' --allow-origin-override
```

`--insert` persists the origin first and **exits 2 without queueing anything**
if the stored origin conflicts with `--mode`.

`--insert` now **requires** `--mode` and **refuses to run unscoped** — it must be
given `--clip-key` or `--session-id`. This is the guard rail against the
accidental mass-queue that happened during earlier testing.

## n8n import notes

Both workflows ship **inactive** with **no embedded credentials**.

1. Reselect the PostgreSQL credential on every Postgres node.
2. Define variables: `KBC_CLIPS_DEST_DRIVE_ID`, `KBC_CLIPS_DEST_FOLDER_ITEM_ID`,
   and optionally `KBC_LIVE_WINDOW_DAYS` (default 21).
3. Producer: *Persist Session Media Origin* takes `$1..$3` = `session_ids`,
   `mode`, `allow_override`; *Queue Positive Clip Jobs* takes `$1..$5` = dest
   drive id, dest folder item id, `session_ids`, `clip_keys`, `produced_by`.
   The *Origin Accepted?* IF node routes `conflict` / `not_found` to
   *Stop On Origin Conflict*, so nothing is queued under the wrong priority.
4. Reconciliation: *Create Missing Media Jobs* takes the same `$1..$5`;
   *Requeue Retryable Failed Jobs* takes `$1` = `job_ids` (`{}` = all retryable);
   *Report Unresolved Origin* takes `$1` = `live_window_days` (diagnostic only).
5. Activating reconciliation with empty `session_ids` will queue **every**
   outstanding V5 clip. That is its job — but confirm the backlog is intended
   before enabling the schedule.

**SQL pasted into n8n must contain no comments.** The Postgres node splits
queries on `;` and rewrites `$n`, and both appear inside comments. Every file
destined for n8n is generated comment-free; two production incidents so far
traced to this.

## Live lecture contract

For a genuinely new lecture in the normal production flow:

1. V5 finishes and saves `positive_clips` with
   `clips_analysis_completeness = 'positive_clips_v5_final'`.
2. Call the producer sub-workflow with `session_id` and `mode = 'live'`.
3. The producer persists `clips_media_origin = 'live'` **first**, then creates
   jobs at priority 100.

If step 3's insert fails after the origin was saved, hourly reconciliation sees
`clips_media_origin = 'live'` and creates the missing jobs at priority 100. No
date guess is involved. This is the whole reason the origin is persisted on the
lecture.

For historical backfill the same call is made with `mode = 'history'`, whatever
the lecture date happens to be.

## Worker

Unchanged: concurrency 1, no database credentials, no Microsoft credentials,
outbound HTTPS to n8n only.
