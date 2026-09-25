# Legacy n8n discovery compatibility

The production-equivalent upstream path is:

```text
Calendar -> Teams online meeting resolution -> active Aptem Group match -> QA One Lecture
```

It discovers calendar lectures before `qa_doctors_sessions` exists. Consequently that QA table must never be used as the primary discovery registry.

No live workflow was exported or modified for Phase 1. Existing sanitized repository exports include:

- `automation/positive_clips/n8n/kbc-positive-clips-producer-v3.json`
- `automation/positive_clips/n8n/kbc-positive-clips-reconciliation.json`
- `automation/lecture_parts/n8n/kbc-lecture-parts-complete-node-snippets.json`

Recording links: `QA Master Daily — Safe Exact Recording v8` is the production export (workflow `8yeJigbj8BCNBMkU`). Its only drift from production is that production has `Execute QA One Lecture` disabled. `QA_Master_Daily_Safe_Exact_Recording_v9.json` is the repo-only successor, generated from `recording_v9/`. See `recording_v9/README.md`.

Those files describe downstream compatibility only. They are not copied here, and this directory contains no credentials.
