"""
Phase 2C4: deterministic learner engagement.

Built only from persisted Phase 2C3 evidence - the frozen attendance snapshot,
speaker identity resolutions and deterministic speaker roles. The live
external attendance table is never read by the normal calculation.

  calculator.py  pure legacy-parity arithmetic and participant derivation
  service.py     orchestration and persistence

No QA table is written and no AI is run. The Item 7 result is a deterministic
recommendation for Phase 3, not a checklist write.
"""
