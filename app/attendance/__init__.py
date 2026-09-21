"""
Phase 2C3: attendance resolution and deterministic speaker roles.

Two deliberately separate layers:

  roster.py          effective attendance roster + provenance fingerprint
  legacy_matching.py verbatim port of the legacy `isSimilar` comparator
  resolver.py        speaker -> attendance-member person resolution
  roles.py           deterministic VTT-derived speaker roles

Person identity and speaker role are different concepts with different
versions. A change to the matching algorithm must never silently rewrite
stored trainer/learner evidence, so nothing here writes back to
lecture_transcript_cues or lecture_transcript_speakers.

No engagement metric is calculated in this phase.
"""
