# Lecture Three-Part Splitting — Planner v1

A separate media subsystem. It reuses the proven `qa_media_jobs` queue, the
local FFmpeg worker and the n8n Media Worker API unchanged, and shares **no**
tables or code with the positive-clip pipeline.

```
Lecture
  -> Transcript (Microsoft Graph, WEBVTT)
  -> Split Planner        code parses, builds two candidate windows
  -> AI                   picks TWO cue IDs + writes titles. No timestamps.
  -> Deterministic Validator
  -> qa_lecture_split_plans
  -> (later) 3 x qa_media_jobs   job_type=lecture_part, cut_mode=fast_copy
  -> existing FFmpeg worker -> SharePoint
  -> qa_lecture_part_assets
```

## Division of responsibility

| | |
|---|---|
| **AI decides** | `cut_1_cue`, `cut_2_cue`, two reasons, three titles/summaries, a confidence |
| **Code decides** | everything numeric: parsing, cue→timestamp resolution, all three ranges, every validation |

The AI never sees a timestamp it could copy and never returns one. It is given
only cue IDs and dialogue text.

## Boundary convention

> `boundary_seconds` = the **start** timestamp of the selected boundary cue.

```
Part 1:  0              -> cut_1_seconds
Part 2:  cut_1_seconds  -> cut_2_seconds
Part 3:  cut_2_seconds  -> recording_duration_seconds
```

Because adjacent parts share the identical boundary value, the three parts are
contiguous **by construction** — a gap or an overlap is arithmetically
impossible. The validator asserts this anyway.

Silence and keyframe refinement are deliberately out of scope for v1. With
`cut_mode = fast_copy` FFmpeg will land on the nearest keyframe at or before the
requested time, which is acceptable for lecture parts; Phase 2 can refine.

## Balancing

| | target | allowed zone |
|---|---|---|
| cut 1 | 33% | **25% – 42%** |
| cut 2 | 66% | **58% – 75%** |

Backstop: every part must be **15% – 50%** of the lecture. The zones already
imply part 1 and part 3 land in 25–42% and part 2 in 16–50%, so the backstop
only catches anything unexpected.

The AI is told to prefer topic transitions, exercise boundaries, break
boundaries, end of Q&A, recap boundaries and explicit trainer transition
phrases — and, where several cues are equally good, the one nearest the middle
of the window, which keeps the parts balanced.

## Transcript handling

The full transcript is parsed deterministically in code. Only two bounded
windows are sent to the AI — `WINDOW_CUES_EACH_SIDE = 40`, so at most ~81 cues
per window. On the 600-cue fixture that is **27% of the transcript**, and the
fraction shrinks as lectures get longer, so request size stays predictable for
a multi-hour lecture.

`cue_id` is the 1-based ordinal of the cue in the WEBVTT file, which matches the
numbering the existing V5 positive-clip analysis already uses — cue IDs mean the
same thing in both subsystems.

## Deterministic validation

A plan is accepted only if **all** of these hold:

1. `cut_1_cue` and `cut_2_cue` are integers that exist in the transcript
2. both were among the candidate cues actually offered (the AI cannot reach outside its window)
3. `cut_1_cue < cut_2_cue`
4. cut 1 time falls inside 25–42%; cut 2 time inside 58–75%
5. `cut_2_seconds > cut_1_seconds`, and `recording_duration_seconds > cut_2_seconds`
6. all three part durations are > 0
7. parts are contiguous — no gaps, no overlaps
8. every part is within 15–50% of the lecture
9. exactly three parts, numbered 1/2/3 once each
10. confidence, if present, is between 0 and 1

Any failure ⇒ `planner_status = 'rejected'`, the errors are recorded, and
`preview_media_jobs()` returns **zero** jobs. Invalid AI output is never
silently accepted.

## Recording duration — the current blocker

Part 3 ends at the true end of the recording, so an accurate duration is
mandatory.

`qa_doctors_sessions.duration` is free text at minute granularity
(`"2 hours 13 minutes"`) describing the *meeting*, not the recording file. Using
it would truncate or overrun every part 3, so it is **not** used.

Migration 101 adds the interface:

```sql
qa_doctors_sessions.recording_duration_seconds numeric(12,3) NULL
```

Populate it from **Graph `driveItem.video.duration`** (milliseconds ÷ 1000) when
the recording link is resolved, or from `ffprobe`. It is deliberately NULL
everywhere — no value has been fabricated. `121_split_candidates.sql` reports
`blocked_missing_duration` so the gap is visible.

## Identities

| Thing | Rule | Example |
|---|---|---|
| `split_plan_key` | `split:<md5(session_id)[:8]>:<split_version>` | `split:9dabbf79:semantic_balanced_v1` |
| job key | `lecturepart:<md5(session_id)[:8]>:<split_version>:<n>` | `lecturepart:9dabbf79:semantic_balanced_v1:2` |
| filename | `YYYYMMDD_<safe-subject>_<md5(session_id)[:8]>_part-0N.mp4` | `20260914_..._9dabbf79_part-02.mp4` |
| asset identity | `UNIQUE (session_id, split_version, part_number)` | |

`md5(session_id)[:8]` is the same short id the positive-clip filenames already
use. Part filenames end `part-0N`, which cannot collide with the clip pattern
`clip-NN_cue-X-Y`.

Re-planning is supported: a new `split_version` produces a new plan row and a
new set of job keys, leaving the previous version's delivered assets intact.

## Origin and priority

Reuses the existing semantics unchanged — `qa_doctors_sessions.clips_media_origin`:

| origin | priority |
|---|---|
| `live` | 100 |
| `history` | 10 |

Lecture-part jobs sit in the same queue and are claimed by the same
priority-ordered claim query. Positive-clip priority behaviour is untouched.

## SharePoint destination

Lecture parts must **not** land in the Positive Clips folder. Two variables:

```
KBC_LECTURE_PARTS_DEST_DRIVE_ID
KBC_LECTURE_PARTS_DEST_FOLDER_ITEM_ID
```

The **drive** may safely be the same drive as the positive-clip destination —
the drive is just the document library, and both already resolve through the
same n8n upload-session step. The **folder item id must be different**, and it
has not been invented here: create the "Lecture Parts" folder in SharePoint and
supply its real item id.

## COMPLETE / FAIL endpoint integration (not yet applied)

Future chain:

```
Mark Media Job Completed
  -> Ensure Completion Accepted
  -> Sync Positive Clip Asset     (existing, unchanged)
  -> Sync Lecture Part Asset      (NEW - sql/assets/110)
  -> Return Completed Result
```

`Sync Lecture Part Asset` acts only on `job_type = 'lecture_part'`; a
positive_clip job falls out of its WHERE clause and it returns zero rows — a
no-op, not an error. It upserts on `(session_id, split_version, part_number)`,
is idempotent on replay, and only ever writes when `output_item_id` and
`output_web_url` are both present, so a completed part can never be downgraded.

`Fail Lecture Part Asset` (sql/assets/111) is UPDATE-only and guarded by
`status IS DISTINCT FROM 'completed' AND output_item_id IS NULL`, so a failed
retry cannot destroy a delivered part and a failure never creates an asset row.

Both are in `n8n/kbc-lecture-parts-complete-node-snippets.json` as copy-ready
nodes. That workflow is a carrier only — **do not activate it**.

**SQL pasted into n8n must be comment-free**: the Postgres node splits on `;`
and rewrites `$n`, both of which appear in comments. The generated snippets are
already stripped and asserted `;`-free.

## Status view

`public.qa_lecture_split_pipeline_status` — derived, nothing stored. One row per
lecture with `media_origin`, `split_version`, `planner_status`, part counts
(`parts_total/pending/processing/completed/failed/exhausted`), `assets_completed`
and a `pipeline_status` of `not_planned` / `planning_failed` / `planned` /
`queued` / `processing` / `completed` / `partially_failed`.

## Files

```
planner.py                                       parser, windows, AI contract, validator, previews
sql/migrations/100_create_lecture_split_tables.sql   applied
sql/migrations/101_add_recording_duration_seconds.sql applied
sql/assets/110_sync_lecture_part_asset.sql       COMPLETE node, needs manual paste
sql/assets/111_fail_lecture_part_asset.sql       FAIL node, needs manual paste
sql/monitoring/120_lecture_split_pipeline_status_view.sql  applied
sql/monitoring/121_split_candidates.sql          read-only candidate report
n8n/kbc-lecture-parts-complete-node-snippets.json copy-ready nodes, inactive
```
