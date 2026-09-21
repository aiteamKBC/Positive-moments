BEGIN;

CREATE TABLE IF NOT EXISTS public.lecture_sessions (
    lecture_id uuid PRIMARY KEY,
    source_system text NOT NULL DEFAULT 'MICROSOFT_GRAPH_CALENDAR',
    calendar_user_upn text NOT NULL,
    calendar_event_id text NOT NULL,
    i_cal_uid text,
    meeting_id text,
    join_url text NOT NULL,
    subject text NOT NULL,
    normalized_subject text NOT NULL,
    module text,
    scheduled_start timestamptz NOT NULL,
    scheduled_end timestamptz NOT NULL,
    session_date date NOT NULL,
    calendar_timezone text,
    organizer_upn text,
    graph_meeting_subject text,
    graph_meeting_start timestamptz,
    graph_meeting_end timestamptz,
    meeting_type text,
    calendar_mapping_status text NOT NULL,
    group_match_status text NOT NULL,
    discovery_status text NOT NULL,
    is_cancelled boolean NOT NULL DEFAULT false,
    first_discovered_at timestamptz NOT NULL DEFAULT now(),
    last_discovered_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT lecture_sessions_time_order CHECK (scheduled_end > scheduled_start),
    CONSTRAINT lecture_sessions_mapping_status CHECK (calendar_mapping_status IN
        ('EXACT_JOIN_URL_MATCH', 'ONLINE_MEETING_NOT_FOUND', 'AMBIGUOUS_ONLINE_MEETING')),
    CONSTRAINT lecture_sessions_group_status CHECK (group_match_status IN
        ('MATCHED', 'NO_ACTIVE_GROUP_MATCH', 'MISSING_MEETING_ID')),
    CONSTRAINT lecture_sessions_discovery_status CHECK (discovery_status IN
        ('READY', 'UNMATCHED', 'REVIEW')),
    CONSTRAINT lecture_sessions_event_key UNIQUE
        (source_system, calendar_user_upn, calendar_event_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS lecture_sessions_ical_key
    ON public.lecture_sessions (source_system, lower(calendar_user_upn), i_cal_uid)
    WHERE i_cal_uid IS NOT NULL;
CREATE INDEX IF NOT EXISTS lecture_sessions_business_day_idx
    ON public.lecture_sessions (session_date, normalized_subject, scheduled_start);
CREATE INDEX IF NOT EXISTS lecture_sessions_meeting_id_idx
    ON public.lecture_sessions (meeting_id) WHERE meeting_id IS NOT NULL;

COMMENT ON TABLE public.lecture_sessions IS
    'Canonical shadow registry of calendar lecture occurrences; independent of transcript and legacy QA session IDs.';

CREATE TABLE IF NOT EXISTS public.lecture_discovery_runs (
    run_id uuid PRIMARY KEY,
    target_date date NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    mode text NOT NULL DEFAULT 'SHADOW',
    calendar_events_found integer NOT NULL DEFAULT 0,
    teams_events_found integer NOT NULL DEFAULT 0,
    online_meetings_resolved integer NOT NULL DEFAULT 0,
    active_group_matches integer NOT NULL DEFAULT 0,
    unmatched_count integer NOT NULL DEFAULT 0,
    error_count integer NOT NULL DEFAULT 0,
    status text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT lecture_discovery_runs_mode CHECK (mode IN ('SHADOW', 'DRY_RUN')),
    CONSTRAINT lecture_discovery_runs_status CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED')),
    CONSTRAINT lecture_discovery_runs_counts CHECK (
        calendar_events_found >= 0 AND teams_events_found >= 0
        AND online_meetings_resolved >= 0 AND active_group_matches >= 0
        AND unmatched_count >= 0 AND error_count >= 0
    )
);

CREATE INDEX IF NOT EXISTS lecture_discovery_runs_date_idx
    ON public.lecture_discovery_runs (target_date, started_at DESC);

COMMENT ON TABLE public.lecture_discovery_runs IS
    'Audit summaries for coded lecture discovery shadow runs; contains no credentials or tokens.';

COMMIT;
