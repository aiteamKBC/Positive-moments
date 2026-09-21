-- ===========================================================================
-- Migration 003: canonical registry scope, meeting-context provenance, and
--                organizer identity validation.
--
-- 1. CANONICAL SCOPE. public.lecture_sessions is the canonical KBC LECTURE
--    registry, not a mirror of the Teams calendar. Only non-cancelled Teams
--    occurrences whose normalized subject exactly matches an ACTIVE Aptem
--    group belong here; everything else lives in discovery-run diagnostics.
--    The scope is now enforced by the database, not only by the service.
--
-- 2. MEETING CONTEXT PROVENANCE. The onlineMeetings user context is the
--    configured discovery mailbox's Graph object ID. The Oid embedded in a
--    JoinWebUrl is an internal, documented-as-unstable format; it is recorded
--    only as a secondary hint status, never as the production dependency.
--
-- 3. ORGANIZER VALIDATION. The calendar event's organizer is a Microsoft 365
--    Unified Group (Teams cohort) mailbox, NOT the Teams meeting organizer
--    user. Comparing it to a meeting organizer was structurally meaningless.
--    organizer_upn is therefore renamed to calendar_organizer_address and kept
--    purely as calendar metadata, and validation now compares
--    onlineMeeting.participants.organizer.identity.user.id to the user context.
--
-- 4. DOWNSTREAM READINESS. An occurrence whose meeting did not resolve stays a
--    distinct canonical lecture (occurrences are never merged or deleted by
--    subject+start) but is explicitly not eligible for downstream processing.
--
-- No token, Authorization header, or credential is stored by any column here.
-- ===========================================================================

BEGIN;

ALTER TABLE public.lecture_sessions
    RENAME COLUMN organizer_upn TO calendar_organizer_address;
ALTER TABLE public.lecture_sessions
    RENAME COLUMN organizer_object_id TO meeting_organizer_user_id;

ALTER TABLE public.lecture_sessions
    ADD COLUMN IF NOT EXISTS meeting_lookup_user_id text,
    ADD COLUMN IF NOT EXISTS meeting_lookup_context_source text,
    ADD COLUMN IF NOT EXISTS join_url_oid_hint_status text,
    ADD COLUMN IF NOT EXISTS downstream_ready boolean NOT NULL DEFAULT false;

-- Values written by the removed calendar-organizer-vs-meeting-organizer
-- comparison describe a check that no longer exists. They are cleared rather
-- than translated: the next discovery run recomputes them from the meeting's
-- own organizer user ID.
UPDATE public.lecture_sessions
   SET organizer_validation_status = NULL
 WHERE organizer_validation_status IN
       ('ORGANIZER_MATCH', 'ORGANIZER_MISMATCH', 'ORGANIZER_UNVERIFIED');

ALTER TABLE public.lecture_sessions
    DROP CONSTRAINT IF EXISTS lecture_sessions_organizer_validation;
ALTER TABLE public.lecture_sessions
    ADD CONSTRAINT lecture_sessions_organizer_validation CHECK (
        organizer_validation_status IS NULL OR organizer_validation_status IN (
            'ORGANIZER_ID_CONFIRMED', 'ORGANIZER_ID_DIFFERENT',
            'ORGANIZER_ID_UNAVAILABLE', 'NOT_ATTEMPTED'
        )
    );

ALTER TABLE public.lecture_sessions
    DROP CONSTRAINT IF EXISTS lecture_sessions_meeting_context_source;
ALTER TABLE public.lecture_sessions
    ADD CONSTRAINT lecture_sessions_meeting_context_source CHECK (
        meeting_lookup_context_source IS NULL OR meeting_lookup_context_source IN (
            'DISCOVERY_MAILBOX_OBJECT_ID', 'JOIN_URL_OID_FALLBACK'
        )
    );

ALTER TABLE public.lecture_sessions
    DROP CONSTRAINT IF EXISTS lecture_sessions_oid_hint_status;
ALTER TABLE public.lecture_sessions
    ADD CONSTRAINT lecture_sessions_oid_hint_status CHECK (
        join_url_oid_hint_status IS NULL OR join_url_oid_hint_status IN (
            'JOIN_URL_OID_ABSENT', 'JOIN_URL_OID_MATCHES_CONTEXT',
            'JOIN_URL_OID_DIFFERS_FROM_CONTEXT'
        )
    );

-- Canonical scope, enforced. An unmatched calendar event cannot become a row.
ALTER TABLE public.lecture_sessions
    DROP CONSTRAINT IF EXISTS lecture_sessions_group_status;
ALTER TABLE public.lecture_sessions
    ADD CONSTRAINT lecture_sessions_canonical_scope CHECK (group_match_status = 'MATCHED');

ALTER TABLE public.lecture_sessions
    DROP CONSTRAINT IF EXISTS lecture_sessions_discovery_status;
ALTER TABLE public.lecture_sessions
    ADD CONSTRAINT lecture_sessions_discovery_status CHECK (discovery_status IN ('READY', 'REVIEW'));

-- Downstream eligibility requires a resolved meeting.
ALTER TABLE public.lecture_sessions
    DROP CONSTRAINT IF EXISTS lecture_sessions_downstream_ready;
ALTER TABLE public.lecture_sessions
    ADD CONSTRAINT lecture_sessions_downstream_ready CHECK (
        downstream_ready = false OR meeting_id IS NOT NULL
    );

CREATE INDEX IF NOT EXISTS lecture_sessions_downstream_ready_idx
    ON public.lecture_sessions (session_date) WHERE downstream_ready;

COMMENT ON TABLE public.lecture_sessions IS
    'Canonical KBC lecture registry: non-cancelled Teams calendar occurrences whose normalized subject exactly matches an active Aptem group. Not a mirror of the Teams calendar.';
COMMENT ON COLUMN public.lecture_sessions.calendar_organizer_address IS
    'Calendar metadata: the Microsoft 365 Unified Group (Teams cohort) mailbox that owns the event. NOT the Teams meeting organizer and never a user context.';
COMMENT ON COLUMN public.lecture_sessions.meeting_organizer_user_id IS
    'onlineMeeting.participants.organizer.identity.user.id of the resolved meeting.';
COMMENT ON COLUMN public.lecture_sessions.meeting_lookup_user_id IS
    'Graph user object ID the onlineMeetings lookup ran as. Never a UPN, never a token.';
COMMENT ON COLUMN public.lecture_sessions.meeting_lookup_context_source IS
    'How that user context was chosen: the configured discovery mailbox (primary) or a guarded JoinWebUrl Oid fallback.';
COMMENT ON COLUMN public.lecture_sessions.join_url_oid_hint_status IS
    'Secondary diagnostic only: whether the internal-format JoinWebUrl Oid agreed with the user context. Resolution never depends on it.';
COMMENT ON COLUMN public.lecture_sessions.downstream_ready IS
    'True only when this occurrence has a resolved onlineMeeting. Unresolved occurrences are retained, never merged or deleted, and stay ineligible downstream.';
COMMENT ON COLUMN public.lecture_sessions.session_date IS
    'Business date: timezone-aware scheduled_start converted to Africa/Cairo, then date(). An occurrence may overlap the queried Cairo day yet legitimately carry the previous Cairo date.';

COMMIT;
