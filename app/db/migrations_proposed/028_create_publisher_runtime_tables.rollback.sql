-- Rollback for PROPOSED migration 028. Refuses while decisions or alerts exist:
-- those are operator records, never removed as a side effect.
BEGIN;

DO $$
BEGIN
    IF to_regclass('public.bunny_publisher_part_decisions') IS NOT NULL
       AND EXISTS (SELECT 1 FROM public.bunny_publisher_part_decisions) THEN
        RAISE EXCEPTION '028 rollback refused: bunny_publisher_part_decisions has rows';
    END IF;
    IF to_regclass('public.bunny_publisher_alerts') IS NOT NULL
       AND EXISTS (SELECT 1 FROM public.bunny_publisher_alerts) THEN
        RAISE EXCEPTION '028 rollback refused: bunny_publisher_alerts has rows';
    END IF;
END $$;

DROP VIEW IF EXISTS public.bunny_publisher_exceptions_v;
DROP VIEW IF EXISTS public.bunny_publisher_lecture_coverage_v;
DROP TABLE IF EXISTS public.bunny_publisher_reconciliation_runs;
DROP TABLE IF EXISTS public.bunny_publisher_alerts;
DROP TABLE IF EXISTS public.bunny_publisher_schedule;
DROP TABLE IF EXISTS public.bunny_publisher_part_decisions;

COMMIT;
