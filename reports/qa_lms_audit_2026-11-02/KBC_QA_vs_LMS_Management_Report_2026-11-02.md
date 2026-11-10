# Historical Lectures: QA vs LMS Data Audit

*Kent Business College · read-only audit of production data, 2 November 2026 · scope: lectures 1 Feb – 31 Oct 2026*

## Executive summary

We examined **780 historical lectures** recorded by the QA process between February and October 2026. A further 22 cancelled lectures are reported separately. For each lecture we searched the current LMS live-session tables, the older (legacy) LMS attendance data, and the Publisher's own lecture and matching records.

| Category | Lectures | Share |
|---|---:|---:|
| **A. Verified LMS match** | **103** | 13.2% |
| **B. LMS session exists but unmatched** | **16** | 2.1% |
| **C. Possible LMS match, needs review** | **600** | 76.9% |
| · of which: recorded only in the legacy LMS | 595 | 76.3% |
| · of which: candidate in the current LMS | 5 | 0.6% |
| **D. No LMS session found** | **18** | 2.3% |
| **E. Source data incomplete** | **43** | 5.5% |
| Cancelled (kept separate) | 22 | – |

**The main finding: most historical lectures did take place in the LMS, but in the legacy LMS, not the current one.**
- The current LMS (the one the Publisher delivers videos to) only began holding real live sessions from June 2026, and holds very few before October.
- For **595 lectures**, the legacy LMS records a session for **the same course module on the same date**. In 589 of these the module name matches exactly.
- The legacy LMS stores no session ID, start time or Teams meeting link. Its records therefore cannot be proven to be a specific lecture under the approved matching rule, and there is no current LMS session to attach a video to.

**The earlier "599 of 615" figure is numerically correct but easy to misread.**
- It is correct that 599 of the 615 non-cancelled QA records for February–August have no session in the **current** LMS on the same Teams meeting and date. Counted as distinct lectures, after merging one duplicate QA record, this is 598 of 614.
- It is **not** correct to read it as "these lectures have no LMS session". 549 of those 598 lectures have a matching legacy-LMS session on the same date. Only 2 are genuinely missing, in a period where the LMS data is complete. The other 43 fall in a period where the data is too thin to tell, and 4 have a weaker current-LMS candidate.

## How the data was checked

| Source | What it holds | Coverage found |
|---|---|---|
| QA database: `public.qa_doctors_sessions` | One row per analysed lecture. Fields include the date (Cairo), subject, trainer, LMS module name, Teams meeting ID and transcript ID. There is no start time. | 803 rows, 2 Feb – 30 Oct 2026. Every row has a Teams meeting ID, across 79 distinct meetings (each a recurring series). |
| Current LMS: `curriculum.live_session_occurrences` + `live_sessions` + `modules` | Scheduled LMS sessions with ID, start and end time, Teams meeting, programme, cohort, group and tutor. **This is the only place the Publisher can deliver a video to.** | Every row was created between **30 Jul and 31 Oct 2026**. Real (non-test) sessions by month: Feb–May **0**, Jun 3, Jul 9, Aug 16, Sep 34, Oct 101. |
| Legacy LMS: `Learner.source_lms_attendance` | Learner attendance per lecture: date, module name and lecture title. No session ID, time or Teams meeting. *Only counts were exported.* | Continuous from 2025: about 70–110 sessions per month from Feb to Aug 2026, then fading out in Oct 2026. |
| Combined attendance: `public.kbc_combined_attendance` (LMS database) | Merges legacy and current-LMS attendance. | **No current-LMS attendance at all from January to June 2026**, only legacy. Current-LMS records start in July, and dominate from September. |
| Publisher: `lecture_sessions`, `bunny_lms_match_evaluations`, `bunny_lms_occurrence_links` | Canonical lectures and stored LMS match results. | The lecture registry starts in September 2026. Only 46 match results are stored (the Publisher matches only its own lookback window). |
| Also checked | `Audit.activity_offerings` (planned activities, not live sessions); `curriculum.source_lms_activities` (videos and quizzes, no live lectures). | Not session sources. |

**How each category was decided:**
- **A (verified)** is the Publisher's approved matching rule (`meeting_key_start_v2`: same Teams meeting at the exact scheduled start), run in memory on every Publisher lecture. Nothing was saved. For 75 of the 103 verified lectures the Publisher's scheduler has not yet recorded a result itself.
- **B and C** are evidence for review only. They were never treated as proven.
- **Matching QA to the Publisher** uses Teams meeting ID plus Cairo date, the same rule the Historical Backfill uses.

## Monthly breakdown (non-cancelled lectures)

| Month | QA lectures | A. Verified | B. Exists, unmatched | C. Possible | D. None found | E. Incomplete |
|---|---:|---:|---:|---:|---:|---:|
| Feb 2026 | 85 | 0 (0%) | 0 (0%) | 81 (95.3%) | 0 (0%) | 4 (4.7%) |
| Mar 2026 | 90 | 0 (0%) | 0 (0%) | 81 (90.0%) | 0 (0%) | 9 (10.0%) |
| Apr 2026 | 85 | 0 (0%) | 0 (0%) | 73 (85.9%) | 0 (0%) | 12 (14.1%) |
| May 2026 | 89 | 0 (0%) | 0 (0%) | 79 (88.8%) | 0 (0%) | 10 (11.2%) |
| Jun 2026 | 73 | 0 (0%) | 3 (4.1%) | 63 (86.3%) | 0 (0%) | 7 (9.6%) |
| Jul 2026 | 109 | 0 (0%) | 6 (5.5%) | 102 (93.6%) | 0 (0%) | 1 (0.9%) |
| Aug 2026 | 83 | 0 (0%) | 7 (8.4%) | 74 (89.2%) | 2 (2.4%) | 0 (0%) |
| Sep 2026 | 67 | 21 (31.3%) | 0 (0%) | 45 (67.2%) | 1 (1.5%) | 0 (0%) |
| Oct 2026 | 99 | 82 (82.8%) | 0 (0%) | 2 (2.0%) | 15 (15.2%) | 0 (0%) |
| **Total** | **780** | **103 (13.2%)** | **16 (2.1%)** | **600 (76.9%)** | **18 (2.3%)** | **43 (5.5%)** |

**What drives the shape of this table:**
- **February to May:** the current LMS holds **no real sessions at all**, so no lecture can be verified. Almost every lecture is recorded in the legacy LMS.
- **June to August:** two Project Controls courses (Ray's PMP June 2026 cohort and Steve's Earned Value Management) already had current-LMS sessions on the **same Teams meeting at the same time**. Those 16 lectures are category B. Everything else is legacy-only.
- **September:** the transition month. 21 lectures are verified, and 45 are still recorded only in the legacy LMS for courses not yet moved to the current LMS.
- **October:** 82.8% verified. The 15 "none found" are courses that have no current-LMS module at all (see below).

**QA data checks:**
- One duplicate QA record (February) was merged.
- 22 lectures were cancelled.
- Every QA record has a usable Teams meeting ID.
- No February–August lecture exists in the Publisher. All September and October lectures do.

## Course coverage

82 QA course modules were examined. They fall into four groups:

| Coverage | Course modules | Meaning |
|---|---:|---|
| **Fully verified** | 12 | Every lecture verified in the current LMS. All are Sep–Oct cohorts of Marketing Manager L6, Marketing Executive L4 and Project Controls Professional L6. |
| **Partial current-LMS coverage** | 2 | *Ray, PMP June 2026* (8 verified, 10 same-meeting but unmatched) and *Steve, EVM 2026* (8 verified, 6 unmatched). |
| **Legacy LMS only** | 58 | The lectures are recorded in the legacy LMS only, never in the current LMS. This is the bulk of February–August. |
| **No LMS data in either system** | 10 | Mostly non-apprenticeship or external courses. |

**Largest legacy-only courses** (QA lectures with a same-date, same-module legacy LMS session):
- G1 and G2, Ray, Project Management Professional, Jan 26: 28 and 26
- PMP, Dr Amgad, Thursday: 18
- Dr Fredah, Strategy & Planning, Jan 26: 17
- Dr Fredah, Impact & Planning, Jan 26: 16
- Juliane, Impact & Planning, June 2026 (two groups): 16 each
- Keith, Strategy & Planning, June 2026 (two groups): 15 each
- Femi, Strategy & Planning, Jan 26: 15
- Prof Charl, Impact & Planning and Strategy & Planning, Jan 26: 15 each
- Ray, MSP: 14
- Andrew, Risk Management: 14
- The Femi, Keith and Nathan Customer Journey and Commercial Intelligence cohorts: 12–14 each

**Courses with no LMS data in either system:**
- *Strategic Human Resources* (17 lectures, Feb–Jun)
- *Project Control Professional Level 6 – Al Fanar* (12) and *PCP Al-Fanar 2025* (1)
- *Primavera Course* (7 of 10 lectures)
- *procurement Risk* (2)
- *Advanced Project and Logistics Management* (7, Oct)
- *AI in Project Control 2026* (4 of 7)
- *Stephen, Portfolio Management 2026* (3 of 14)
- *Sufian, Strategic Leadership* (1 of 6; the other 5 have only weak *MBA – Strategic Leadership* candidates)

The Al-Fanar, Primavera and Strategic HR courses look like corporate or non-LMS deliveries. This is a hypothesis that the LMS team should confirm.

**LMS sessions without a QA lecture.** Most LMS occurrences in August–October belong to test programmes, such as "Final Test", "Rewan 2 test", "Test fouda" and "TEST Final Prog". Many of these are **not** on the Publisher's configured list of test programmes, which is worth tidying up. Among real sessions:
- 14 *MBA – Strategic Leadership* sessions have no QA lecture on the same Teams meeting. Five of them fall on the same Saturdays as QA's *Sufian – Strategic Leadership* lectures, but on a different Teams meeting. These are flagged as weak "possible" matches.
- The rest are a handful of single sessions (see the *LMS Session Inventory* and *LMS Programme Coverage* sheets).

## Examples

- **A, verified:** 2 Sep 2026, *Andrew – Scheduling Professional (SP) Jan 2026*, matched to LMS session `OCC-4EB9399ABBCA46E98E3085DF5B3B5428` (Project Controls Professional Level 6).
- **B, exists but unmatched:** 10 Jun 2026, *Ray – PMP June 2026*. LMS session `OCC-D171A448FB39427E9F0103DEC4E064F1` is on the same Teams meeting at 08:00 UTC. It is unmatched because the lecture is not in the Publisher registry (the Teams calendar no longer lists it), so there is no calendar start time for the approved rule to check.
- **C, legacy LMS only:** 4 Feb 2026, *G1 – Ray – Project Management Professional – Jan 26*. The legacy LMS has the same module on the same date, lecture "L1: Introduction to the Programme". It has no session ID or start time, and there is no current-LMS session.
- **C, current LMS, weak:** 11 Jul 2026, *Sufian – Strategic Leadership*. The current LMS has *MBA – Strategic Leadership* on the same Saturday, on a different Teams meeting.
- **D, none found:** 5 Oct 2026, *Advanced Project and Logistics Management*. There is no module of this kind in either LMS.
- **E, incomplete:** 14 Feb 2026, *Strategic Human Resources*. The current LMS has no data for February, and the legacy LMS has no attendance for this course.

## Why there is an apparent gap

**Confirmed facts:**
1. The current LMS live-session feature was populated from **30 July 2026**: every row has a creation date between 30 Jul and 31 Oct. Older occurrences were back-dated for test programmes only. There are no real current-LMS sessions before June 2026, and very few before October.
2. In `kbc_combined_attendance`, the LMS's own combined attendance shows **zero** current-LMS attendance from January to June 2026, and legacy attendance throughout. The college ran its lectures through the legacy LMS until the migration, which happened course by course between June and October 2026.
3. The legacy LMS has no session identity: no ID, start time or Teams meeting. A lecture can only be found there by date plus module name.
4. Even in September, many lectures ran on Teams meetings that the current LMS did not create, because their course had not yet moved.
5. Current-LMS data was never discarded, deleted or truncated by pagination. The audit read the tables directly with no row limits.

**Hypotheses, not confirmed:**
- No historical sessions were back-filled into the current LMS when courses migrated, and none exist anywhere else in the LMS platform.
- Al-Fanar, Primavera and Strategic HR were delivered outside the LMS.

**Matching restrictions confirmed by this audit:**
- The approved rule needs the lecture's exact start time from the Teams calendar.
  - That is unavailable for every February–August lecture.
  - It would also prevent the 16 category-B lectures from being verified, even though the LMS has a session on the same meeting at a plausible time.
- No QA lecture has more than one legacy candidate, so there is no ambiguity on that side.

## Impact on Historical Backfill

- **Can be published today through the normal pipeline:** the 103 verified lectures (September and October). Their recordings still need to be found and checked in the usual way.
- **Cannot be published without a business decision:** the 16 lectures in category B and the 600 in category C.
- **Cannot be published at all:** the 18 lectures in category D and the 43 in category E. No LMS session is known for them.
- **About February–August:** under the current rules, Historical Backfill will show these lectures, which is correct, but cannot publish any of them, because there is no current-LMS session to deliver to.

## Decision options

| Option | Lectures affected | Benefits | Risks |
|---|---:|---|---|
| **1. Keep current rules: publish only verified lectures** | 103 now | No change and no identity risk. | About 600 historical recordings stay unpublished. |
| **2. Approve a manual mapping for the 16 category-B lectures** (operator confirms each against its LMS session) | 16 | Same Teams meeting and plausible start time; low risk; small, auditable set. | Needs a manual-mapping step with a review record, which is a new operator workflow. |
| **3. Create current-LMS sessions for legacy-only lectures** (the LMS team back-fills historical sessions, then the normal rule verifies them) | Up to 595 (589 with exact module match) | No change to Publisher rules. Every video gets a real LMS session and appears where learners look. | LMS effort. Each new session needs the correct Teams meeting and start time, otherwise the approved rule still won't verify it. Learners would see old cohorts' sessions. |
| **4. Approve a "legacy evidence" rule** (QA record + legacy module/date match = publishable) | Up to 589 | Fast; uses existing data. | There is no session to attach a video to in the current LMS. It weakens identity: same date and module name, no time or meeting. **Not recommended without option 3.** |
| **5. Publish to a non-LMS archive** (for example a library page per course) | Up to about 660 (all non-cancelled lectures without a verified match) | Recordings become available without LMS identity work. | A new delivery channel outside the LMS contract, needing separate access-control and safeguarding review. |
| **6. Declare February–August out of scope** | 614 | Zero effort and risk. | The historical recordings stay unavailable. |

The **18 "none found"** and **43 "incomplete"** lectures need an LMS-team answer before any option applies to them. Each course needs one of three answers: an LMS course exists, it was delivered outside the LMS, or it's out of scope.

## Recommended next steps

1. **Business owner:** choose the scope for February–August: option 3, 5 or 6. Option 4 should not be used on its own.
2. **LMS team:**
   - confirm that no historical sessions exist elsewhere in the LMS platform;
   - confirm how the 10 courses with no LMS data were delivered;
   - say whether historical sessions can be back-filled, with the correct Teams meeting and start time.
3. **Operations:** decide on option 2 for the 16 Ray PMP / Steve EVM lectures.
4. **Data hygiene, independent of the decision:**
   - add the obvious test programmes to the Publisher's configured test list;
   - tidy the QA module names (HTML escapes like `&amp;`, broken dashes, double spaces), which complicate matching.
5. **Publisher:** let the scheduler evaluate the 75 verified-in-memory September and October lectures it has not yet checked. A confirmed Historical Backfill run for September–October does this through the normal pipeline.

## Files

- `KBC_QA_vs_LMS_Audit_2026-11-02.xlsx`, 12 sheets:
  - Executive Summary
  - Monthly QA vs LMS Coverage
  - All QA Lectures – Row Level (all 802, including cancelled)
  - Verified LMS Matches
  - LMS Exists but Unmatched
  - Possible LMS Matches
  - No LMS Session Found (D and E)
  - LMS Session Inventory
  - Legacy LMS Sessions
  - Course & Cohort Coverage
  - LMS Programme Coverage
  - Data Quality & Exceptions

  Every sheet has filters and a frozen header row. Teams meeting IDs and QA transcript IDs appear only as short fingerprints, and no learner data is included; legacy attendance appears as counts only.
- **Method:** production databases read under READ-ONLY sessions. No records, rules, code or services were changed.
