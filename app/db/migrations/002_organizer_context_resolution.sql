-- ===========================================================================
-- Migration 002: organizer-context meeting resolution.
--
-- The discovery calendar mailbox is the DISCOVERY context only. Each lecture's
-- onlineMeeting is now resolved in its own organizer's user context, so the
-- registry records which organizer was used and whether the meeting Graph
-- returned agrees with the calendar event's organizer.
--
-- Also widens calendar_mapping_status, because organizer resolution introduces
-- outcomes the original CHECK did not allow, and because meeting lookup is now
-- skipped entirely for events that did not match an active Aptem group.
-- ===========================================================================

BEGIN;

ALTER TABLE public.lecture_sessions
    ADD COLUMN IF NOT EXISTS organizer_object_id text,
    ADD COLUMN IF NOT EXISTS organizer_validation_status text;

ALTER TABLE public.lecture_sessions
    DROP CONSTRAINT IF EXISTS lecture_sessions_mapping_status;
ALTER TABLE public.lecture_sessions
    ADD CONSTRAINT lecture_sessions_mapping_status CHECK (calendar_mapping_status IN (
        'RESOLVED',
        'ONLINE_MEETING_NOT_FOUND',
        'AMBIGUOUS_ONLINE_MEETING',
        'ORGANIZER_NOT_RESOLVABLE',
        'FORBIDDEN_FOR_ORGANIZER',
        'NOT_ATTEMPTED',
        -- retained so rows written before this migration stay valid
        'EXACT_JOIN_URL_MATCH'
    ));

ALTER TABLE public.lecture_sessions
    DROP CONSTRAINT IF EXISTS lecture_sessions_organizer_validation;
ALTER TABLE public.lecture_sessions
    ADD CONSTRAINT lecture_sessions_organizer_validation CHECK (
        organizer_validation_status IS NULL OR organizer_validation_status IN (
            'ORGANIZER_MATCH', 'ORGANIZER_MISMATCH', 'ORGANIZER_UNVERIFIED', 'NOT_ATTEMPTED'
        )
    );

COMMENT ON COLUMN public.lecture_sessions.organizer_object_id IS
    'Graph object ID of the user context used for the onlineMeetings lookup. Never a UPN.';
COMMENT ON COLUMN public.lecture_sessions.organizer_validation_status IS
    'Cross-check of the meeting organizer Graph returned against the calendar event organizer.';

COMMIT;
