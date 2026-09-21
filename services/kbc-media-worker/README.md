# KBC Media Worker

FFmpeg media worker for the Positive Mentions pipeline. It replaces Creatomate.

It is a **pull worker**: it opens outbound HTTPS connections to n8n and nothing
else. No inbound port is required on the machine that runs it.

```
Docker container (this worker)
        |  outbound HTTPS only
        v
n8n  ->  Microsoft Graph (source recording + SharePoint upload session)
     ->  PostgreSQL  AiTeamKBC / public.qa_media_jobs
```

## What this worker does and does not hold

| | |
|---|---|
| Holds | `KBC_WORKER_SECRET` only (from `.env`) |
| Does **not** hold | Microsoft OAuth tokens, Graph credentials, database credentials |

Every Microsoft Graph URL it handles is a short-lived URL minted by n8n per job.
Those URLs are never logged in full and never reported back in error text; the
logger redacts every query string before it is written.

## Job types

| `job_type` | `cut_mode` | FFmpeg strategy |
|---|---|---|
| `positive_clip` | `precise` | re-encode (`libx264` / `veryfast` / CRF 20, `aac` 160k) for an accurate cut |
| `lecture_part` | `fast_copy` | `-c copy` stream copy, with `part_number` 1-3 |

`lecture_part` is accepted and validated today; the AI segmentation that will
produce those jobs is a later feature and lives outside this worker.

## Configuration

```
cp .env.example .env
```

Then set `KBC_WORKER_SECRET` to the value configured in n8n. `.env` is
git-ignored. The repository-root `.env` belongs to the Django application and is
untouched by this service.

## Validate before running anything

```
docker compose build media-worker
docker compose run --rm --no-deps media-worker node src/config-check.js
```

`config-check` reports the FFmpeg and FFprobe versions inside the image, the
resolved endpoints (redacted), whether the secret is present, and exits non-zero
if anything is missing. It never claims or processes a job.

## Run

```
docker compose up -d --build media-worker      # start
docker compose logs -f media-worker            # watch logs
docker compose down                            # stop
```

On Windows, `run-media-worker.bat` performs the same start after waiting for
Docker Desktop, and `install-windows-task.ps1` registers it as a scheduled task
that runs at logon and re-checks every 30 minutes.

## Job lifecycle

1. `POST` claim endpoint. `{ "job": null }` means idle, wait `IDLE_BACKOFF_SECONDS`.
2. Validate `job_id`, `job_type`, `cut_mode`, `part_number`, `start_seconds`,
   `end_seconds`, `source_download_url`, `output_filename`.
3. FFmpeg cuts into `DATA_DIR/tmp/<job_id>/`, seeking with an HTTP range request
   rather than downloading the whole recording.
4. Verify exit code, that the file exists and is non-trivial, and that the
   FFprobe duration matches the request within tolerance.
5. Request a fresh SharePoint upload session from n8n.
6. Upload in `UPLOAD_CHUNK_BYTES` chunks with `Content-Range`, honouring Graph's
   `nextExpectedRanges` and retrying 429/5xx/416 with backoff.
7. `POST` complete (or fail) and delete the temporary directory either way.

Stale temporary directories from a previous crash are removed at startup.

## Files

```
src/worker.js        poll loop and job lifecycle
src/ffmpeg.js        job validation, FFmpeg cutting, FFprobe verification
src/upload.js        Microsoft Graph resumable upload
src/http.js          JSON POST helper with retry/backoff
src/config.js        environment parsing and validation
src/env.js           dependency-free .env loader
src/redact.js        URL redaction used by logs and error reporting
src/state.js         heartbeat file
src/healthcheck.js   Docker healthcheck
src/config-check.js  dry-run validation, processes no jobs
sql/                 reference DDL for public.qa_media_jobs (already applied)
```

`sql/001_create_media_jobs.sql` is kept for reference only. `qa_media_jobs`
already exists in `AiTeamKBC`; do not run it against a new database.
