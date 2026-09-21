"""
Phase 3C1: the controlled legacy QA writer and its guardrails.

A compatibility adapter, never a source of truth. It takes the Phase 3B
rendered payload and, under an explicitly chosen mode, proposes or performs
writes into the legacy `qa_doctors_*` tables.

  modes.py    the write modes and the ownership/protection policy
  mapping.py  the single place legacy columns are mapped and digested
  service.py  dry-run diffing, atomic writes, verification, rollback

Default mode is DRY_RUN: nothing is written to a legacy table unless a
write-enabled mode is chosen deliberately. The writer never calls a model,
never re-renders, and never recalculates anything.
"""
