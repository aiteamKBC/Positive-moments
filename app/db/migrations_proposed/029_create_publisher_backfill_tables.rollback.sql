-- Rollback for PROPOSED migration 029. Refuses while any historical run exists: runs are the
-- operators' audit record and are never removed as a side effect.
BEGIN;

DO $$
BEGIN
    IF to_regclass('public.bunny_backfill_runs') IS NOT NULL
       AND EXISTS (SELECT 1 FROM public.bunny_backfill_runs) THEN
        RAISE EXCEPTION '029 rollback refused: bunny_backfill_runs has rows';
    END IF;
END $$;

DROP TABLE IF EXISTS public.bunny_backfill_items;
DROP TABLE IF EXISTS public.bunny_backfill_runs;

COMMIT;
