# Positive Clips — media job producer & endpoint SQL

Queues `positive_clip` jobs into `public.qa_media_jobs` for the local FFmpeg
media worker, and synchronises successful results into
`public.qa_positive_clip_assets`.

Creatomate is not used anywhere in this path.

## Architecture

```
V5 analysis completes (qa_doctors_sessions.clips_analysis_completeness
                       = 'positive_clips_v5_final')
        |
        v
n8n  "KBC Positive Clips - Media Job Producer"   <- one Postgres node, pure SQL
        |
        v
public.qa_media_jobs  (status = pending)
        |
        v
local Docker FFmpeg worker  --outbound HTTPS-->  n8n  -->  Graph / SharePoint
        |
        v
n8n complete endpoint  -->  qa_media_jobs.status = completed
                       -->  upsert qa_positive_clip_assets
```

The worker never connects to PostgreSQL and holds no Microsoft credentials.

## Deterministic identities

| Thing | Rule | Example |
|---|---|---|
| `clip_key` | `session_id:start_cue:end_cue` — unchanged from the existing system | `...VjI=:296:304` |
| `job_key` | `positive:<md5(session_id)[:8]>:<start_cue>:<end_cue>` | `positive:f7777a51:296:304` |
| `output_filename` | `YYYYMMDD_<safe-subject>_<md5(session_id)[:8]>_clip-NN_cue-X-Y.mp4` | `20260715_G1-Femi-Customer-Journey-Optimisation_4dc3d901_clip-01_cue-748-748.mp4` |

`md5(session_id)[:8]` is not a new invention — it is the same short id already
present in the 14 existing `qa_positive_clip_assets.clip_filename` values.
No random UUIDs are used for clip identity.

## Trim rule

`start_seconds = max(0, original_start - 60)`, `end_seconds = original_end + 60`,
`cut_mode = precise`, `part_number = NULL`.

Timestamps (`HH:MM:SS.mmm`) are parsed in SQL with a strict regex and PostgreSQL
`interval` arithmetic. Malformed clips are rejected, never guessed.

## Idempotency

A clip is skipped when **either**:

* a `qa_positive_clip_assets` row for the same `clip_key` has `trim_status = 'completed'`, or
* a `qa_media_jobs` row for the same `clip_key` is `pending`, `processing` or `completed`.

A `failed` job is deliberately *not* skipped — it is retried through the same row,
because `ON CONFLICT (job_key) DO NOTHING` plus the `qa_media_jobs_job_key_key`
UNIQUE constraint makes re-running reuse the existing row instead of duplicating it.

## Files

| File | Purpose |
|---|---|
| `sql/010_positive_clip_candidates.sql` | candidate CTE — parsing, keys, filenames, idempotency joins |
| `sql/011_insert_positive_clip_jobs.sql` | idempotent `INSERT ... ON CONFLICT DO NOTHING` |
| `sql/020_complete_endpoint_asset_sync.sql` | COMPLETE endpoint asset upsert |
| `sql/030_fail_endpoint.sql` | FAIL endpoint, with asset-downgrade protection |
| `n8n/kbc-positive-clips-producer.json` | importable producer workflow |
| `run_producer.py` | controlled local runner (preview / insert) |
| `destination.json` | SharePoint destination drive + folder ids (identifiers, not secrets) |

Placeholders must never appear inside SQL comments — psycopg and the n8n
Postgres node bind them there too, which produces `IndeterminateDatatype`.

## Local runner

```
.venv/Scripts/python.exe automation/positive_clips/run_producer.py --preview --one-per-lecture --limit 3
.venv/Scripts/python.exe automation/positive_clips/run_producer.py --insert --clip-key '<clip_key>'
```

**Always scope `--insert` with `--clip-key` or `--session-id`.** An unfiltered
`--insert` queues every eligible clip across all V5 lectures.

The runner reads `DATABASE_URL` from `backend/.env` and never prints it.

## n8n import notes

After importing `n8n/kbc-positive-clips-producer.json`:

1. **Reselect the PostgreSQL credential** on the *Queue Positive Clip Media Jobs*
   node — the JSON ships with an empty `credentials` object by design.
2. Define two n8n variables (Settings → Variables):
   * `KBC_CLIPS_DEST_DRIVE_ID`
   * `KBC_CLIPS_DEST_FOLDER_ITEM_ID`
   Their values are in `destination.json`.
3. The workflow ships **inactive**. Activating it with an empty `session_ids`
   queues every eligible clip in the database.
4. Call it from the V5 analysis workflow with `session_id` so each run is scoped
   to the lecture that just finished.

The Postgres node uses positional `$1..$4` = destination drive id, destination
folder item id, `session_ids` array, `clip_keys` array.

## Complete / fail endpoint wiring

`020` runs in the complete workflow after the job row is marked completed. It
only writes an asset when `output_item_id` and `output_web_url` are both present,
so an asset can never describe an upload that did not land.

`qa_positive_clip_assets` has **two** identities and both are honoured:

* `PRIMARY KEY (clip_key)` - stable clip identity
* `UNIQUE (session_id, source_start, source_end)` - temporal identity

A plain `ON CONFLICT (clip_key)` upsert would raise a unique violation if a
re-analysis produced different cue numbers for the same time range. Instead the
statement resolves an existing asset by **either** identity (exact `clip_key`
match preferred), then updates it in place. `clip_key` is deliberately absent
from the `SET` list, so a row matched only on the temporal identity **keeps its
original `clip_key`** - later cue renumbering never rewrites clip identity.

Race safety: a `pg_advisory_xact_lock` keyed on `session_id|source_start|source_end`
serialises concurrent completions for the same clip, and the `INSERT` carries an
untargeted `ON CONFLICT DO NOTHING` that absorbs **both** unique constraints, so a
duplicate completion can never crash the endpoint.

`030` runs in the fail workflow. Its asset statement is **UPDATE-only and guarded**
by `trim_status IS DISTINCT FROM 'completed' AND clip_url IS NULL`, so a failed
retry can never destroy a previously successful asset, and a failure never
creates an asset row.
