# Phase 3A: shadow QA engine

```
lecture_sessions + selections + combined transcript + canonical cues
lecture_transcript_speaker_roles     (canonical trainer)
lecture_attendance_snapshots (v2)    (frozen roster)
lecture_engagement_metrics           (Item 7, engagement)
        │
        ▼
lecture_qa_evaluations + lecture_qa_checklist_items + lecture_qa_evidence_clips
        │
        ▼
Phase 3B rendering, then Phase 3C writer cutover (neither started)
```

Shadow only. Nothing is written to `qa_doctors_sessions`,
`qa_doctors_checklist_items`, `qa_perfect_lectures` or `qa_doctors_transcripts`,
and no compatibility view is created over them.

Engine version **`shadow_qa_v8_engine_v1`**; prompt **`legacy_qa_v8_prompt_v1`**;
structured schema **`legacy_qa_v8_structured_v1`**.

## 1. Model connection — read from the graph, not the names

The export has two OpenAI model nodes, and the node names do not say which is
which. Following the `ai_languageModel` connections:

| Node | Connected to | Model |
| --- | --- | --- |
| `OpenAI Chat Model (Combined)` | `AI Teaching QA Combined` (the QA agent) | **gpt-5.2** |
| `OpenAI Chat Model` | `Combined Structured Output Parser` (auto-fix) | **gpt-4.1** |

The agent node sets `retryOnFail: true` and `waitBetweenTries: 5000` (n8n's
default `maxTries` is 3). Both model ids are recorded in `app/qa/prompt.py`;
the QA model is the one the engine calls, and the fixer model is recorded but
not reimplemented — a response that will not parse is an explicit
`INVALID_STRUCTURED_OUTPUT` result rather than a second silent model call.

**Live validation (Phase 3A.1).** gpt-5.2 has now been called for real; the
provider reported the snapshot `gpt-5.2-2025-12-11`. No model was substituted
and no fallback provider exists. See §12.

## 2. Prompt and schema

`app/qa/prompt.py` is generated from the export, not hand-written. It carries
the system message verbatim (10,392 characters, SHA-256
`b0b9b38853697a4c08d6c471bd25c10f4ec16b1f77e29dc478c17d7a2ef2c01a`), the
structured schema verbatim (SHA-256 `20370279…`), and a user-message builder
that reproduces the legacy template exactly — including its blank lines and
the space before the colon in `startDifferenceMinutes : `.

`prompt_sha256` hashes the prompt version, the system message, the schema
version and hash, and the model id together, so changing the model changes the
prompt contract and therefore the QA provenance.

## 3. KSB framework — audited, and empty

`KSB_Framework` appears in exactly one place in the whole export: the agent's
own prompt text, as `${$json.KSB_Framework || ""}`. **No node assigns it**, so
it always rendered empty. The platform sends it empty for the same reason.
Nothing was invented to fill it and no external source was consulted.

## 4. LMS dependency — deferred to Phase 3B

`Get LMS Students` queries `kbc_users_data`, but its output flows only to
`Merge1`/`Merge2`, which feed `Upsert QA Session` and
`Upsert QA Checklist Items`. The agent prompt references no LMS field. LMS data
therefore affects **output shape, not model input**, so Phase 3A does not query
`kbc_users_data` at all and `lms_module` / `lms_students` /
`lms_students_count` are Phase 3B's problem.

## 5. Deterministic layer

| Rule | Source | Behaviour |
| --- | --- | --- |
| Delivery gate | node `Session Delivered?` | `durationMinutes < 20` must be **false** to reach the model, so exactly 20 is delivered. Under 20: no AI call, ever. |
| Non-delivered output | node `Cancelled Session Output` | 11 rows all `Not Met`, counts 0/0/11, trainer `Session not delivered`, Engagement 0, `0 minutes`, duration_score 0, engagement_score 0, teaching-quality rating **1** (not 0). |
| Item 1 | system message | Met ≥ 105, Partially Met 95–104, Not Met < 95. Not the 120 used by duration_score. |
| Item 2 | system message | Met when start ≤ +20 and end ≥ −20. Early start and overrun are always fine. |
| Item 7 | Phase 2C4 | The persisted `learner_engagement_status` wins whenever `item7_override_applied`; otherwise the model's answer stands, as legacy did with no attended learners. |
| duration_score | node `Build Session and Checklist Rows` | > 120 → 5, ≥ 115 → 4, ≥ 110 → 3, ≥ 105 → 2, else 1. Note 120 exactly scores 4. |

Every checklist row stores both `status` and `ai_status` plus a
`status_source`, so a model answer can never silently replace a deterministic
verdict, and the disagreement stays visible.

**Trainer policy.** `canonical_trainer` is the Phase 2C3 VTT top speaker. The
model's `session_info.trainer` is stored only as `ai_suggested_trainer`. The
legacy precedence `aiTrainer || lecturerFromVtt || lectureRow.trainer` is
deliberately **not** reproduced: it let a model string overwrite a speaker
identity the platform derived from evidence.

## 6. Structured output and evidence validation

The schema is enforced again in code, because JSON that parses can still be
unusable. Rejected: missing or extra top-level keys, a checklist that is not
exactly 11 rows in the exact order with the exact item strings, a status
outside the three allowed values, more than 3 clips, a rating outside 1–5, an
unknown KSB type.

Every evidence clip is checked against the canonical Phase 2C1 cue timeline and
stored with a status: `VALID`, `OUT_OF_TRANSCRIPT_RANGE`, `NO_CUE_OVERLAP`,
`SHORTER_THAN_MINIMUM` (the legacy 2-second rule), `END_NOT_AFTER_START`, or
`UNPARSEABLE_TIMESTAMP`. Invalid clips are recorded, never repaired. Evidence
*text* rendering (the legacy `blocksFromRange` 1500 ms merge) is Phase 3B.

## 7. Provenance and cost control

`source_fingerprint` is a SHA-256 over: engine version, prompt version and
hash, model, lecture, selection, combined transcript fingerprint and hash,
document id and parser fingerprint, schedule and actual timing, duration,
canonical trainer speaker id, attendance snapshot id, and the engagement id and
its fingerprint. It is the table's unique key, so a material change creates a
new evaluation and never overwrites an old one.

A rerun with the same fingerprint, prompt, model and engine **reuses** the
stored successful evaluation and makes no call. `--force` re-evaluates.
Retries are bounded (3 attempts, 5 s apart, no retry on a 4xx other than
rate-limiting) and never fall back to another model.

Statuses: `PENDING`, `NON_DELIVERED`, `COMPLETED`, `MODEL_ERROR`,
`INVALID_STRUCTURED_OUTPUT`, `INVALID_EVIDENCE`, `REVIEW_REQUIRED`. A provider
failure is never converted into `Not Met` verdicts.

## 8. Real run — 2026-09-04

Seven lectures, one gated out by duration:

| Lecture | Delivery | Duration | AI called | Item 1 | Item 2 | Final Item 7 | duration_score |
| --- | --- | ---: | --- | --- | --- | --- | ---: |
| AI in Project Control 2026 | **NON_DELIVERED** | 13 | **no** | — | — | Not Met (cancelled) | 0 |
| Femi-Commercial Intelligence | DELIVERED | 144 | **yes** | Met | Met | Not Met | 5 |
| G2-Juliane | DELIVERED | 133 | **yes** | Met | Met | Met | 5 |
| PPC \| Andrew | DELIVERED | 210 | **yes** | Met | Not Met | Met | 5 |
| Ray-PMO | DELIVERED | 123 | **yes** | Met | Met | Not Met | 5 |
| G3 Femi | DELIVERED | 125 | **yes** | Met | Met | Partially Met | 5 |
| Ray-MSP | DELIVERED | 125 | **yes** | Met | Met | Partially Met | 5 |

AI in Project Control proves the intended separation: it is a perfectly valid
discovered lecture with a canonical transcript and an engagement result, and it
is still **not** a QA-deliverable session. Provider calls for it: 0. No legacy
QA row was created.

## 9. Legacy deterministic parity

| Field | Parity |
| --- | ---: |
| session id (provider transcript id) | **6 / 6** |
| meeting id | **6 / 6** |
| trainer | **6 / 6** |
| duration text | **6 / 6** |
| attended count | **6 / 6** |
| Engagement % | **6 / 6** |
| engagement_score | **6 / 6** |
| Item 1 | **6 / 6** |
| Item 7 | **6 / 6** |
| Item 2 | **1 / 6** |

### Item 2 — a legacy defect, not a regression

Legacy's own Item 2 text records its inputs, and they are **exactly 180 minutes
more negative than ours on every lecture, at both ends**:

| Lecture | new start / end | legacy start / end | offset |
| --- | --- | --- | ---: |
| Femi-Commercial | −1 / +25 | −181 / −155 | 180 |
| G2-Juliane | −1 / +12 | −181 / −168 | 180 |
| G3 Femi | −12 / −2 | −192 / −182 | 180 |
| PPC \| Andrew | −133 / −43 | −313 / −223 | 180 |
| Ray-MSP | −6 / +7 | −186 / −173 | 180 |
| Ray-PMO | −1 / +37 | −181 / −143 | 180 |

Cause, from the export: `Select Transcript Parts` → `scheduledToUtcMs()` parses
a naive scheduled timestamp **as UTC** ("QA Master sends UTC without trailing
Z"), while QA Master requests `Prefer: outlook.timezone="Africa/Cairo"`. Cairo
is UTC+3, so every scheduled instant was pushed 3 hours later and every session
looked as if it had ended hours early — which is why legacy marked Item 2
`Not Met` on 5 of 6 lectures with reasoning like "ended 155 minutes early" for a
session that actually overran by 25 minutes.

The one match (PPC) agrees by coincidence: it genuinely ended 43 minutes early,
so both readings say Not Met.

Phase 2B already computes Cairo-correct timing, so our Item 2 is right and
legacy's was wrong. Parity is **not** forced to the legacy value.

This is the first real-data sighting of the carried Master-vs-One-Lecture
timezone divergence, in punctuality metadata. It does **not** resolve the
carried risk, which is about transcript *selection* on an ambiguous
multi-candidate occurrence — a different failure mode that remains untested.

## 12. Live model validation (Phase 3A.1)

Six delivered lectures called gpt-5.2; the 13-minute lecture called nothing.
All six now hold `COMPLETED` evaluations with 255 validated evidence clips.

**Two real model behaviours the validator caught, and what each meant:**

1. **Trailing-whitespace item variants — a defect in our validator, now fixed.**
   The legacy system message states the eleven items twice, and items 1, 2 and
   10 carry trailing spaces in the "MUST MATCH EXACTLY" list but not in the
   JSON template. The model echoes either list. The legacy production table
   holds *both* spellings for items 2 and 10 on 2026-09-04, so legacy accepted
   this too. `checklist_item_match` now treats a surrounding-whitespace
   difference as `WHITESPACE_ONLY` - reported in
   `metadata.checklist_item_whitespace_variants`, never an error - while any
   other text difference is still `CHECKLIST_ITEM_MISMATCH`. The canonical
   string is what gets persisted, so output identity stays stable.
2. **Genuine model deviations, correctly rejected.** One response used KSB
   types `"Skills"`/`"Behaviours"` (the legacy schema enumerates
   `Knowledge`/`Skill`/`Behaviour`), and one used two 0.76 s evidence clips
   against the prompt's 2-second rule. Both were recorded as explicit error
   states rather than accepted or repaired, and both cleared on retry. The
   legacy auto-fixing model (gpt-4.1) is still deliberately not reimplemented.

**Deterministic overrides held on real output.** The model answered `Met` for
Item 7 on all six lectures; the stored final status came from Phase 2C4 and
differs on five of them. Item 1 and Item 2 each overrode the model once. The
model's `session_info.trainer` matched the canonical trainer exactly on all
six, and was still stored only as `ai_suggested_trainer`.

**AI-only checklist agreement with legacy: 47 / 48** (items 3-6, 8-11), with
teaching-quality rating 4 on all six in both systems.

**Cost behaviour.** A failed evaluation is retried on the next run by design;
a successful one is never re-called. The final reuse run made **0 provider
calls** and left every row byte-identical. Whether `INVALID_EVIDENCE` should
be terminal rather than retryable is a policy question for Phase 3C.

## 10. Still open

1. **Real multi-part fixture validated: NO.**
2. **Master vs One Lecture timezone divergence** not exercised on a real
   ambiguous multi-candidate occurrence — now known to have corrupted legacy
   punctuality metadata, still untested for selection.
3. ~~No QA model credentials~~ — **CLOSED** by Phase 3A.1: real gpt-5.2 calls
   validated end to end (§12).

## 11. Phase 3B entry point

Read `lecture_qa_evaluations` plus its checklist rows and evidence clips.

- Render evidence text from `lecture_qa_evidence_clips` with the legacy
  `blocksFromRange` rules (same speaker, gap ≤ 1500 ms, max 8 blocks, 260
  characters), joining cue text at render time so names stay out of the QA
  engine.
- Add the LMS snapshot (`lms_module`, `lms_students`, `lms_students_count`)
  from `kbc_users_data`, read-only.
- Build the legacy session and checklist output shape, including the
  `session_id_match` key, still without writing to the legacy tables.
- Decide how Item 2 is presented, given that the new value is correct and the
  historical legacy value is not.
