-- Rollback for PROPOSED migration 027. Refuses while any discovery row exists:
-- emptying recording state is an operator decision, never a side effect.
BEGIN;

DO $$
BEGIN
    IF to_regclass('public.bunny_recording_discoveries') IS NOT NULL
       AND EXISTS (SELECT 1 FROM public.bunny_recording_discoveries) THEN
        RAISE EXCEPTION '027 rollback refused: bunny_recording_discoveries has rows';
    END IF;
    IF to_regclass('public.bunny_recording_sources') IS NOT NULL
       AND EXISTS (SELECT 1 FROM public.bunny_recording_sources) THEN
        RAISE EXCEPTION '027 rollback refused: bunny_recording_sources has rows';
    END IF;
END $$;

DROP VIEW IF EXISTS public.bunny_recording_part_sources_v;
DROP VIEW IF EXISTS public.bunny_recording_coverage_v;
DROP TABLE IF EXISTS public.bunny_recording_sources;
DROP TABLE IF EXISTS public.bunny_recording_discoveries;

COMMIT;
