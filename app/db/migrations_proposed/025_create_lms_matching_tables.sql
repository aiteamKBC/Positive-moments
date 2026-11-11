-- ===========================================================================
-- PROPOSED migration 025: LMS occurrence matching and two-sided coverage.
--
-- STATUS: PROPOSED, NOT APPROVED FOR PRODUCTION (see 024's header for why it
-- lives in app/db/migrations_proposed/). Rollback:
-- 025_create_lms_matching_tables.rollback.sql. Requires nothing from 024.
--
-- Purely additive: three tables and one view; nothing existing is altered.
--
-- SOURCES OF TRUTH
-- ----------------
-- docs/architecture/kent_lms_bunny_publisher_audit.md (sections 5, 11, 13)
-- docs/architecture/lms_matching_gap_investigation.md (sections 6, 9)
--
-- The LMS database is a DIFFERENT database and is only ever read. LMS ids are
-- stored as plain text with their provenance; there are no cross-database
-- foreign keys. The authoritative per-lecture LMS identity is
-- curriculum.live_session_occurrences.id.
--
-- MODEL
-- -----
-- bunny_lms_match_evaluations  one row per DISTINCT decision for a lecture.
--     Re-evaluating to the same decision only bumps last_evaluated_at; a
--     different decision supersedes the current row (is_current=false) and
--     inserts a new one. Exactly one current row per lecture.
-- bunny_lms_occurrence_links   the occurrences a VERIFIED decision names:
--     one PRIMARY, plus SHARED_MODULE rows when the LMS explicitly shares the
--     meeting between modules (curriculum.module_teams_shares). An occurrence
--     belongs to at most one lecture's current decision.
-- bunny_lms_occurrence_coverage        the LMS side: every in-window occurrence and
--     whether an LI lecture covers it, so LMS-only sessions surface instead of
--     disappearing (investigation section 9.2).
--
-- No URL, join link, passcode, Graph meeting id or email is stored: meetings
-- are identified by meeting_key_hash (sha256 prefix of thread id + message
-- id), and a CHECK refuses URL-like text in every jsonb column.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.bunny_lms_match_evaluations (
    evaluation_id             uuid PRIMARY KEY,
    lecture_id                uuid NOT NULL
        REFERENCES public.lecture_sessions(lecture_id) ON DELETE RESTRICT,
    rule_version              text NOT NULL,
    match_status              text NOT NULL,
    requires_review           boolean NOT NULL,
    is_current                boolean NOT NULL DEFAULT true,

    lecture_scheduled_start   timestamptz NOT NULL,
    meeting_key_hash          text,
    candidate_count           integer NOT NULL DEFAULT 0,
    -- sha256 of (rule_version, status, links, candidate ids): the decision
    -- itself, so a re-evaluation can tell "same answer" from "new answer".
    decision_sha256           text NOT NULL,
    -- Candidate occurrence ids, exclusion counts, reasons. Never URLs.
    evidence                  jsonb NOT NULL DEFAULT '{}'::jsonb,

    first_evaluated_at        timestamptz NOT NULL DEFAULT now(),
    last_evaluated_at         timestamptz NOT NULL DEFAULT now(),
    evaluation_count          integer NOT NULL DEFAULT 1,
    superseded_at             timestamptz,
    superseded_by             uuid REFERENCES public.bunny_lms_match_evaluations(evaluation_id)
                                  ON DELETE RESTRICT,

    -- FK target for the links: lets is_current cascade, and pins a link to a
    -- VERIFIED decision of the SAME lecture.
    CONSTRAINT bunny_lms_match_evaluations_link_key
        UNIQUE (evaluation_id, lecture_id, match_status, is_current),

    CONSTRAINT bunny_lms_match_evaluations_status CHECK (match_status IN (
        'VERIFIED',                -- exactly one active occurrence (+ explicit shares)
        'AMBIGUOUS',               -- several active candidates; never take the first
        'RESCHEDULED_CANDIDATE',   -- same meeting, different start within 7 days
        'NO_LMS_SESSION',          -- the meeting is not in the LMS
        'LMS_RECORD_INACTIVE',     -- only cancelled occurrences / superseded or cancelled series match
        'CONFLICT',                -- same meeting key, contradicting online meeting id,
                                   -- or the occurrence is already another lecture's
        'STALE',                   -- was VERIFIED, the LMS changed; never re-pointed automatically
        'NO_MEETING_KEY',          -- the lecture carries no parsable Teams meeting
        'TEST_EXCLUDED',           -- matched only occurrences of configured test programmes
        'LI_DUPLICATE',            -- LI suppressed this lecture as a duplicate calendar entry
        'OUT_OF_LMS_SCOPE')),      -- explicitly declared outside the LMS (operator decision)
    CONSTRAINT bunny_lms_match_evaluations_review CHECK (
        requires_review = (match_status IN ('AMBIGUOUS', 'RESCHEDULED_CANDIDATE', 'NO_LMS_SESSION',
                                            'LMS_RECORD_INACTIVE', 'CONFLICT', 'STALE', 'NO_MEETING_KEY'))),
    CONSTRAINT bunny_lms_match_evaluations_current CHECK (
        is_current = (superseded_at IS NULL)
        AND (superseded_by IS NULL OR NOT is_current)),
    CONSTRAINT bunny_lms_match_evaluations_rule CHECK (length(rule_version) BETWEEN 1 AND 64),
    CONSTRAINT bunny_lms_match_evaluations_key CHECK (
        meeting_key_hash IS NULL OR meeting_key_hash ~ '^[0-9a-f]{16}$'),
    CONSTRAINT bunny_lms_match_evaluations_sha CHECK (decision_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT bunny_lms_match_evaluations_counts CHECK (
        candidate_count >= 0 AND evaluation_count >= 1 AND last_evaluated_at >= first_evaluated_at),
    CONSTRAINT bunny_lms_match_evaluations_evidence CHECK (
        jsonb_typeof(evidence) = 'object' AND evidence::text !~* '(https?://|tempauth|[?&]sig=|@thread\.)')
);

-- Exactly one current decision per lecture; history is the other rows.
CREATE UNIQUE INDEX IF NOT EXISTS bunny_lms_match_evaluations_one_current
    ON public.bunny_lms_match_evaluations (lecture_id) WHERE is_current;
CREATE INDEX IF NOT EXISTS bunny_lms_match_evaluations_status_idx
    ON public.bunny_lms_match_evaluations (match_status, last_evaluated_at) WHERE is_current;
CREATE INDEX IF NOT EXISTS bunny_lms_match_evaluations_history_idx
    ON public.bunny_lms_match_evaluations (lecture_id, first_evaluated_at);

COMMENT ON TABLE public.bunny_lms_match_evaluations IS
    'One row per distinct LMS-match decision for a lecture; one current row per lecture. LMS ids are text (different database). No URLs or meeting ids.';


CREATE TABLE IF NOT EXISTS public.bunny_lms_occurrence_links (
    evaluation_id             uuid NOT NULL,
    lecture_id                uuid NOT NULL,
    match_status              text NOT NULL,
    is_current                boolean NOT NULL,

    lms_occurrence_id         text NOT NULL,
    lms_live_session_id       text NOT NULL,
    lms_module_catalogue_id   text,
    lms_session_number        integer,
    lms_scheduled_start       timestamptz NOT NULL,
    link_role                 text NOT NULL,
    -- For SHARED_MODULE: the module whose meeting is shared (the LMS source).
    shared_source_module_id   text,
    created_at                timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (evaluation_id, lms_occurrence_id),
    -- is_current follows the decision (ON UPDATE CASCADE); a link can only
    -- hang off a VERIFIED decision of the same lecture.
    CONSTRAINT bunny_lms_occurrence_links_evaluation_fk
        FOREIGN KEY (evaluation_id, lecture_id, match_status, is_current)
        REFERENCES public.bunny_lms_match_evaluations (evaluation_id, lecture_id, match_status, is_current)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT bunny_lms_occurrence_links_verified CHECK (match_status = 'VERIFIED'),
    CONSTRAINT bunny_lms_occurrence_links_role CHECK (link_role IN ('PRIMARY', 'SHARED_MODULE')),
    CONSTRAINT bunny_lms_occurrence_links_share CHECK (
        (link_role = 'SHARED_MODULE') = (shared_source_module_id IS NOT NULL)),
    CONSTRAINT bunny_lms_occurrence_links_ids CHECK (
        length(lms_occurrence_id) BETWEEN 1 AND 128 AND length(lms_live_session_id) BETWEEN 1 AND 128)
);

-- One LMS occurrence -> at most one lecture (current decisions).
CREATE UNIQUE INDEX IF NOT EXISTS bunny_lms_occurrence_links_one_lecture
    ON public.bunny_lms_occurrence_links (lms_occurrence_id) WHERE is_current;
-- One PRIMARY per decision; SHARED_MODULE rows are extra, never instead.
CREATE UNIQUE INDEX IF NOT EXISTS bunny_lms_occurrence_links_one_primary
    ON public.bunny_lms_occurrence_links (evaluation_id) WHERE link_role = 'PRIMARY';
CREATE INDEX IF NOT EXISTS bunny_lms_occurrence_links_lecture_idx
    ON public.bunny_lms_occurrence_links (lecture_id) WHERE is_current;

COMMENT ON TABLE public.bunny_lms_occurrence_links IS
    'LMS occurrences named by a VERIFIED match decision: one PRIMARY, plus SHARED_MODULE rows only where curriculum.module_teams_shares links the modules. An occurrence maps to one lecture.';


CREATE TABLE IF NOT EXISTS public.bunny_lms_occurrence_coverage (
    lms_occurrence_id         text PRIMARY KEY,
    lms_live_session_id       text NOT NULL,
    lms_module_catalogue_id   text,
    lms_programme_name        text,
    lms_series_status         text NOT NULL,
    lms_meeting_type          text NOT NULL,
    lms_occurrence_status     text NOT NULL,
    lms_scheduled_start       timestamptz NOT NULL,
    lms_scheduled_end         timestamptz NOT NULL,
    meeting_key_hash          text,

    rule_version              text NOT NULL,
    scope_status              text NOT NULL,
    coverage_status           text NOT NULL,
    matched_lecture_id        uuid
        REFERENCES public.lecture_sessions(lecture_id) ON DELETE RESTRICT,
    evidence                  jsonb NOT NULL DEFAULT '{}'::jsonb,

    first_seen_at             timestamptz NOT NULL DEFAULT now(),
    last_seen_at              timestamptz NOT NULL DEFAULT now(),
    status_changed_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT bunny_lms_occurrence_coverage_scope CHECK (scope_status IN ('IN_SCOPE', 'TEST_EXCLUDED', 'INACTIVE')),
    CONSTRAINT bunny_lms_occurrence_coverage_status CHECK (coverage_status IN (
        'NOT_YET_DUE',              -- in scope, has not ended + grace yet
        'MATCHED',                  -- a current VERIFIED lecture links it
        'LMS_ONLY_PENDING',         -- ended, no LI lecture yet, inside the discovery grace
        'LMS_SESSION_NOT_DISCOVERED',  -- ended long enough ago: an LI discovery gap
        'TEST_EXCLUDED',
        'INACTIVE')),               -- cancelled occurrence or superseded/cancelled/test series
    CONSTRAINT bunny_lms_occurrence_coverage_scope_status CHECK (
        (scope_status = 'TEST_EXCLUDED') = (coverage_status = 'TEST_EXCLUDED')
        AND (scope_status = 'INACTIVE') = (coverage_status = 'INACTIVE')),
    CONSTRAINT bunny_lms_occurrence_coverage_matched CHECK (
        (coverage_status = 'MATCHED') = (matched_lecture_id IS NOT NULL)),
    CONSTRAINT bunny_lms_occurrence_coverage_window CHECK (lms_scheduled_end >= lms_scheduled_start),
    CONSTRAINT bunny_lms_occurrence_coverage_key CHECK (
        meeting_key_hash IS NULL OR meeting_key_hash ~ '^[0-9a-f]{16}$'),
    CONSTRAINT bunny_lms_occurrence_coverage_evidence CHECK (
        jsonb_typeof(evidence) = 'object' AND evidence::text !~* '(https?://|tempauth|[?&]sig=|@thread\.)')
);

CREATE INDEX IF NOT EXISTS bunny_lms_occurrence_coverage_status_idx
    ON public.bunny_lms_occurrence_coverage (coverage_status, lms_scheduled_start);
CREATE INDEX IF NOT EXISTS bunny_lms_occurrence_coverage_lecture_idx
    ON public.bunny_lms_occurrence_coverage (matched_lecture_id) WHERE matched_lecture_id IS NOT NULL;

COMMENT ON TABLE public.bunny_lms_occurrence_coverage IS
    'LMS side of the two-sided reconciliation: each observed LMS occurrence and whether an LI lecture covers it. LMS-only sessions surface as LMS_ONLY_PENDING / LMS_SESSION_NOT_DISCOVERED.';


-- Lecture side, derived: every lecture has exactly one LMS-match state.
-- A lecture never evaluated is PENDING, so nothing can be silently absent.
CREATE OR REPLACE VIEW public.bunny_lms_match_status_v AS
SELECT l.lecture_id,
       l.session_date,
       l.scheduled_start,
       coalesce(e.match_status, 'PENDING')      AS match_status,
       coalesce(e.requires_review, false)       AS requires_review,
       e.evaluation_id,
       e.rule_version,
       e.candidate_count,
       e.last_evaluated_at,
       p.lms_occurrence_id                      AS primary_lms_occurrence_id,
       p.lms_live_session_id                    AS primary_lms_live_session_id,
       (SELECT count(*) FROM public.bunny_lms_occurrence_links k
         WHERE k.evaluation_id = e.evaluation_id) AS linked_occurrence_count
  FROM public.lecture_sessions l
  LEFT JOIN public.bunny_lms_match_evaluations e
         ON e.lecture_id = l.lecture_id AND e.is_current
  LEFT JOIN public.bunny_lms_occurrence_links p
         ON p.evaluation_id = e.evaluation_id AND p.link_role = 'PRIMARY';

COMMENT ON VIEW public.bunny_lms_match_status_v IS
    'One LMS-match state per lecture (PENDING when never evaluated). Computed, so it cannot drift from the evaluations.';

COMMIT;
