"""
Phase 3B: legacy QA output rendering.

A renderer, never an evaluator. It turns validated Phase 3A evidence into the
legacy-compatible representation and never calls a model:

  evidence.py       the legacy blocksFromRange / formatClips port
  lms.py            the versioned, read-only LMS learner snapshot
  compatibility.py  the legacy session and checklist payload shapes
  service.py        orchestration and persistence

Output is SHADOW ONLY. Nothing here writes to a qa_doctors_* table.
"""
