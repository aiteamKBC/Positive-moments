# Positive Moments Media — release and deployment

One release: the Recording Link v2 full-recording fix plus the Positive
Moments Media platform. Deploy once, from one exact git SHA.

## What ships

| Area | What it does |
|---|---|
| Recording Link v2 | Resolves a short false start vs the real delivery (`recording_match_v2_full_recording_resolution`); old ambiguity rows re-evaluate automatically. |
| Evidence Intelligence | Canonical cues → recall selector → deterministic validation → independent verifier (`positive_moments_v1_evidence_intelligence`). Provable V5 results are imported instead of re-analysed. |
| Media planning | Consumes the resolved recording (durable drive/item ids), proves the transcript↔file offset from measured duration, plans ±60 s context. |
| Rendering | Creatomate v2 renders API, 1280×720, 25 fps, MP4, trim only. |
| Delivery | Streams the render to one temp file, uploads via a Graph upload session to the existing Positive Clips SharePoint folder. |
| UI | Operations → Positive Moments Media; Lecture → Positive Moments tab. |

## Migration

Exactly one, additive and idempotent: `app/db/migrations/023_create_positive_moment_media_tables.sql`.
It creates eight new `positive_moment_*` tables and alters nothing. The n8n-owned
`qa_media_jobs` and `qa_positive_clip_assets` are untouched.

## Configuration

Only one new value is required from the operator: `CREATOMATE_API_KEY`, in
`/var/www/Positive-moments/backend/config/.env` (never `frontend/.env`, never `VITE_*`).

Optional, with safe defaults (see `backend/.env.example`):
`MEDIA_WEBHOOK_BASE_URL` (public https base of the site; empty = the runner polls),
`MEDIA_WEBHOOK_SECRET` (empty = `DJANGO_SECRET_KEY`), `MEDIA_RENDER_*`,
`POSITIVE_MOMENT_PADDING_*`, `MEDIA_SHAREPOINT_DRIVE_ID` /
`MEDIA_POSITIVE_MOMENTS_FOLDER_ITEM_ID` (empty = `automation/positive_clips/destination.json`),
`POSITIVE_MOMENTS_MODEL_NAME` (empty = `QA_MODEL_NAME`).

**Graph permission for delivery:** uploading needs write access to the Positive Clips
site (`Sites.ReadWrite.All`, `Files.ReadWrite.All`, or `Sites.Selected` with write on that
site) for the existing `MICROSOFT_GRAPH_CLIENT_ID`. Reading source recordings uses the
read access Recording Link already has. Without write access, clips render and then stop
at `FAILED_FINAL / UPLOAD_SESSION_HTTP_403`, which the UI shows. No data is lost, and
Retry works once access is granted.

## Services

A fourth unit, `deploy/systemd/positive-moments-media.service`:

    python -m app.cli.media media-runner

Before installing, align `User`, `WorkingDirectory` and the interpreter path with
`systemctl cat positive-moments-backfill.service`. Concurrency is 1. Restarting it never
queues a render.

## Money safety

* Analysis never renders. "Start analysis" plans clips only.
* Only an explicit **Render** (per moment, per lecture, or "Render all safe clips") moves a
  job to `QUEUED`.
* A restarted runner never submits anything by itself. A submit whose outcome is
  unknown is parked as `SUBMIT_OUTCOME_UNKNOWN`; it is never silently re-submitted.
* A delivered plan (same fingerprint) is never rendered again.

## Read-only readiness check

    python -m app.cli.media positive-moments-preview --from 2026-09-01 --to 2026-09-30

This command runs in a PostgreSQL READ ONLY transaction, makes Graph GETs only, and never
calls the model or Creatomate. It works before migration 023 and reports transcript,
recording and alignment readiness for each lecture.
