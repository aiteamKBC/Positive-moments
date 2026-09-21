# Phase 4C1 — Deterministic duplicate calendar event resolution

## The problem

On 2026-09-18 the calendar held **two** Ray-MSP events for one class:

| | `ea3e1c87…` | `26e74d25…` |
| --- | --- | --- |
| calendar event id | `…AQwXBtwAABA=` | `…ARLM-5EAABA=` |
| iCalUID | `…FE9765E0532A2F4E…` | `…68917E6BDC6F6648…` |
| normalized subject | `ray-managing successful programmes (msp) jan 2026` | *identical* |
| module (active Aptem group) | `Ray-Managing Successful Programmes (MSP) Jan 2026` | *identical* |
| scheduled start / end (UTC) | `11:00` → `13:00` | *identical* |
| meeting | `RESOLVED`, `ORGANIZER_ID_CONFIRMED` | `ONLINE_MEETING_NOT_FOUND` |
| mapping reason | — | `NO_EXACT_JOIN_URL_MATCH_IN_ANY_CONTEXT` |

Phase 4B correctly named this a duplicate booking and stopped, because nothing
in the platform was allowed to decide it. The result was a permanent
`MANUAL_REVIEW_REQUIRED` on a day that was otherwise finished — a review
nobody could ever clear, because re-running discovery produces the same answer
and inventing a meeting id is forbidden.

## Why both rows exist, and must keep existing

Migration 003 is explicit: occurrences are *"never merged or deleted by
subject+start"*. That rule is load-bearing. Two events at the same time with
the same title are sometimes two genuinely different lectures, and a platform
that merged them would silently destroy one. So this phase does not merge and
does not delete. It **annotates**.

## The grouping rule

Two calendar events are candidates only on **exact equality of all five**:

```
(session_date, normalized_subject, module, scheduled_start, scheduled_end)
```

`normalized_subject` is the platform's own `normalize_group` — the same
comparison discovery already used to match the active Aptem group, so the
group key adds no new notion of sameness. `module` is the matched **active
Aptem group**: two events with the same title that matched different groups
are different lectures. Start *and* end must match to the second; "compatible
duration" is implemented as equality, because a tolerance window is a fuzzy
rule wearing a precise costume. Cancelled occurrences are never grouped.

There is no fuzzy title matching, no nearest-start-time and no model anywhere
in this path.

## The winner rule

A **winner** is an occurrence that:

- holds a `meeting_id`, and
- has `calendar_mapping_status` in `RESOLVED` / `EXACT_JOIN_URL_MATCH` (the
  pre-migration-002 spelling of the same fact), and
- has `organizer_validation_status = ORGANIZER_ID_CONFIRMED`.

That last condition is deliberately **stricter than `downstream_ready`**. A
lecture is about to be declared a duplicate of this one; the cost of being too
strict is a manual review that already happens today, and the cost of being
too loose is retiring a real lecture.

A **suppressible** occurrence holds no meeting *and* its mapping status is one
of the final unresolved ones: `ONLINE_MEETING_NOT_FOUND`, `NOT_ATTEMPTED`,
`ORGANIZER_NOT_RESOLVABLE`, `FORBIDDEN_FOR_ORGANIZER`. An unrecognised status
is neither — the rule fails closed.

`AMBIGUOUS_ONLINE_MEETING` is deliberately **not** suppressible. It means Graph
found *several* candidate meetings and we declined to choose: evidence that a
real meeting exists, which is the exact opposite of the emptiness the rule
requires.

## The cases

| Shape | Outcome |
| --- | --- |
| **A** — exactly one winner, every other member suppressible | `DUPLICATE_EVENT_SUPPRESSED`. Automated. |
| **B** — two winners with different meetings | `DUPLICATE_EVENTS_MULTIPLE_VALID_MEETINGS`. Human. |
| **B′** — two winners sharing one meeting id | `DUPLICATE_EVENTS_SHARED_MEETING_ID`. Human. |
| **C** — no winner | `DUPLICATE_EVENTS_NONE_RESOLVED`. Human. |
| **D** — one winner, but a non-suppressible sibling | `DUPLICATE_EVENTS_SIBLING_NOT_SUPPRESSIBLE`. Human, **whole group refused**. |

Case A is the only automated shape, and it is automatable precisely because
there is nothing to choose between: one candidate exists. In cases B, B′ and C
**every member** of the group reports `REVIEW_REQUIRED` — including a member
that resolved. Holding a meeting does not make an occurrence *the* lecture
when the group is undecided, and two events that both ran the pipeline would
each write a legacy row for one real class. Stopping both is the safe
direction to be wrong.

Case D refuses the group **whole** rather than suppressing the clearly-empty
members: a half-decided group is harder to reason about than an undecided one.

The same source event encountered twice (Case D in the brief) needs nothing
new — `lecture_sessions_event_key` and the iCalUID unique index already make
that one row, and the identity is a deterministic UUIDv5.

## What suppression actually is

One key merged into the registry row's existing `metadata`:

```json
"duplicate_suppression": {
  "duplicate_resolution_version": "duplicate_event_resolution_v1",
  "duplicate_resolution_reason": "DUPLICATE_EVENT_SUPPRESSED",
  "winner_lecture_id":  "ea3e1c87-…",  "winner_calendar_event_id": "…",
  "winner_i_cal_uid":   "…",           "winner_meeting_id": "…",
  "suppressed_lecture_id": "26e74d25-…",
  "suppressed_calendar_event_id": "…", "suppressed_i_cal_uid": "…",
  "suppressed_calendar_mapping_status": "ONLINE_MEETING_NOT_FOUND",
  "group_key": { … }, "resolved_at": "2026-09-19T14:43:26.993970+00:00"
}
```

No schema change. `downstream_ready` is restated as `false` in the same
statement. The row keeps its `lecture_id`, its calendar event id, its iCalUID,
its discovery history and every discovery diagnostic it already carried — the
annotation merges, it does not replace.

Three guards sit on the write:

1. the `UPDATE` carries `meeting_id IS NULL`, so a row that acquires a meeting
   between the decision and the write is not suppressed;
2. it carries `NOT (metadata ? 'duplicate_suppression')`, which is the
   idempotency — a settled row is not touched, so `resolved_at` stays the
   moment the decision was actually made and `updated_at` does not churn;
3. the service re-reads the candidate's **downstream footprint** immediately
   before writing and refuses outright if it has any transcript candidate,
   canonical document, evaluation or legacy row. The rule should never select
   such a row — an occurrence with no meeting cannot have a transcript — but
   "should never" is not a guarantee.

The discovery upsert now carries the key across by name, so a nightly
rediscovery cannot erase the decision.

## What a suppressed duplicate looks like

`PipelineStateResolver.for_lecture` answers it and **short-circuits before a
single downstream query**. Every stage is `NOT_APPLICABLE` with reason
`DUPLICATE_EVENT_SUPPRESSED`, `next_action` and `next_executable_action` are
`NOTHING_TO_DO`, `is_complete` is true and `requires_review` is false.

That short-circuit is the point. "A suppressed duplicate cannot enter
downstream processing" is *structural* — the resolver never reads the
transcript, QA or writer tables for it, so there is no rule for fifteen stages
to remember and no path by which one could be forgotten.

Before the annotation exists, a Case A loser reports
`SUPPRESS_DUPLICATE_EVENT` at `DISCOVERY` and `MEETING` with reason
`DUPLICATE_EVENT_PENDING_SUPPRESSION`. `DISCOVERY` had to change too: the
loser's `discovery_status` is `REVIEW`, and `DISCOVERY` comes first in the
resume order, so leaving it alone would have masked the runnable action behind
a review nobody could clear.

## Day reconciliation

A suppressed duplicate gets its **own bucket**, checked first and on the
explicit flag:

- `suppressed_duplicate_count` — visible for audit
- `business_lecture_count` = canonical − suppressed
- not counted as failed, not counted as review, and **not** folded into
  `complete` (it is not a lecture that finished; it is an event that was never
  a lecture)
- excluded from every derived counter, because its `LEGACY_QA_SYNC` stage is
  `NOT_APPLICABLE`, which for a real lecture means "an n8n-owned legacy row"
- still present in `lectures` and in `by_stage`: the source calendar really did
  contain the extra event, and a report that hid it would disagree with the
  registry.

The buckets still partition the day.

## The scheduler

`SUPPRESS_DUPLICATE_EVENT` is lecture-scoped, in `AUTOMATABLE_ACTIONS`, and in
neither `GRAPH_ACTIONS` nor `PROVIDER_ACTIONS` — a cycle with Graph and the
provider switched off can still perform it. It runs under the same per-lecture
advisory lock as every other lecture-scoped action, through the same dispatch,
and the runner re-derives the decision from the registry rather than trusting
the resolver's word for it. Once a row is annotated it reports `NOTHING_TO_DO`,
so later cycles are true no-ops for it.

`ManualReviewRequired` is raised if the rule disagrees with the action it was
handed — the same non-failure path Phase 4B uses for a writer that declines.

## The historical position

The read-only registry-wide scan (`resolve-duplicates --all`, which refuses
`--execute`) found **two** duplicate groups in the entire registry, both the
same recurring Ray-MSP booking, both Case A, **zero** needing a human:

- 2026-09-04 — `22abbd06…` suppressible, winner `39925a6e…`
- 2026-09-18 — `26e74d25…` suppressible, winner `ea3e1c87…`

Only 2026-09-18 was suppressed, because only 2026-09-18 was in scope. The
2026-09-04 pair is left as an operator decision; the scheduler's three-day
lookback will never reach it, so it costs nothing to leave, and broad
historical mutation is out of scope for this phase. One command applies it:

```
python -m app.cli.main resolve-duplicates --date 2026-09-04 --execute
```

## Files

| File | |
| --- | --- |
| `app/lectures/duplicates.py` | the rule. Pure: rows in, decision out. No I/O. |
| `app/db/repositories/lecture_duplicates.py` | the SQL. Three reads, one annotating `UPDATE`, no `DELETE`. |
| `app/lectures/duplicate_service.py` | the only place the two meet. Dry run by default. |
| `app/db/repositories/lecture_sessions.py` | the upsert carries the annotation across. |
| `app/orchestration/stages.py` | `SUPPRESS_DUPLICATE_EVENT`. |
| `app/orchestration/state.py` | the short-circuit, and duplicate-aware `DISCOVERY`/`MEETING`. Its own sibling query is gone. |
| `app/orchestration/runner.py` | the action. |
| `app/orchestration/reconciliation.py` | the bucket and the two counts. |
| `app/cli/main.py` | `resolve-duplicates`. |
