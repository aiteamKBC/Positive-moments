-- ===========================================================================
-- ROLLBACK for PROPOSED migration 025 (LMS matching and coverage).
--
-- Drops only what 025 created. Refuses while any table holds rows: they are
-- the audit trail of which LMS occurrence each lecture was published under.
-- ===========================================================================

BEGIN;

DO $$
DECLARE
    held bigint := 0;
BEGIN
    IF to_regclass('public.bunny_lms_occurrence_links') IS NOT NULL THEN
        SELECT held + count(*) INTO held FROM public.bunny_lms_occurrence_links;
    END IF;
    IF to_regclass('public.bunny_lms_match_evaluations') IS NOT NULL THEN
        SELECT held + count(*) INTO held FROM public.bunny_lms_match_evaluations;
    END IF;
    IF to_regclass('public.bunny_lms_occurrence_coverage') IS NOT NULL THEN
        SELECT held + count(*) INTO held FROM public.bunny_lms_occurrence_coverage;
    END IF;
    IF held > 0 THEN
        RAISE EXCEPTION 'refusing rollback: LMS matching tables hold % row(s)', held;
    END IF;
END $$;

DROP VIEW IF EXISTS public.bunny_lms_match_status_v;
DROP TABLE IF EXISTS public.bunny_lms_occurrence_links;
DROP TABLE IF EXISTS public.bunny_lms_match_evaluations;
DROP TABLE IF EXISTS public.bunny_lms_occurrence_coverage;

COMMIT;
