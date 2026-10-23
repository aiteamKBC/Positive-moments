-- ===========================================================================
-- ROLLBACK for PROPOSED migration 024 (Bunny Stream tracking).
--
-- Drops only the three tables 024 created, in dependency order. It refuses to
-- run while any of them holds rows: those rows are the only record of which
-- Bunny video ids and captions exist, and dropping them would orphan real
-- Bunny objects. Export first (or delete the Bunny objects deliberately), then
-- empty the tables by hand if a rollback is really intended.
-- ===========================================================================

BEGIN;

DO $$
DECLARE
    held bigint := 0;
BEGIN
    IF to_regclass('public.bunny_caption_current') IS NOT NULL THEN
        SELECT held + count(*) INTO held FROM public.bunny_caption_current;
    END IF;
    IF to_regclass('public.bunny_caption_publications') IS NOT NULL THEN
        SELECT held + count(*) INTO held FROM public.bunny_caption_publications;
    END IF;
    IF to_regclass('public.bunny_videos') IS NOT NULL THEN
        SELECT held + count(*) INTO held FROM public.bunny_videos;
    END IF;
    IF held > 0 THEN
        RAISE EXCEPTION 'refusing rollback: Bunny tracking tables hold % row(s)', held;
    END IF;
END $$;

DROP TABLE IF EXISTS public.bunny_caption_current;
DROP TABLE IF EXISTS public.bunny_caption_publications;
DROP TABLE IF EXISTS public.bunny_videos;

COMMIT;
