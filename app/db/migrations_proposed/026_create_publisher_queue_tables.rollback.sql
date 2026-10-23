-- ===========================================================================
-- ROLLBACK for PROPOSED migration 026 (publisher queue, events, heartbeats).
--
-- Drops only what 026 created. Refuses while jobs or events exist: they are
-- the record of what the publisher did. Heartbeats alone do not block it.
-- ===========================================================================

BEGIN;

DO $$
DECLARE
    held bigint := 0;
BEGIN
    IF to_regclass('public.bunny_publisher_events') IS NOT NULL THEN
        SELECT held + count(*) INTO held FROM public.bunny_publisher_events;
    END IF;
    IF to_regclass('public.bunny_publisher_jobs') IS NOT NULL THEN
        SELECT held + count(*) INTO held FROM public.bunny_publisher_jobs;
    END IF;
    IF held > 0 THEN
        RAISE EXCEPTION 'refusing rollback: publisher queue tables hold % row(s)', held;
    END IF;
END $$;

DROP TABLE IF EXISTS public.bunny_publisher_heartbeats;
DROP TABLE IF EXISTS public.bunny_publisher_events;
DROP TABLE IF EXISTS public.bunny_publisher_jobs;

COMMIT;
