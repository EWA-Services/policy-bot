-- Apply once to the selected telemetry Supabase project as the database owner.
create table public.ai_review_events (
    event_id text primary key,
    event_type text not null,
    payload jsonb not null check (jsonb_typeof(payload) = 'object'),
    finding_count integer generated always as ((payload #>> '{counts,total}')::integer) stored,
    admitted_count integer generated always as ((payload #>> '{counts,admitted}')::integer) stored,
    rejected_count integer generated always as ((payload #>> '{counts,rejected}')::integer) stored,
    protected_count integer generated always as ((payload #>> '{counts,protected}')::integer) stored,
    judge_admitted_count integer generated always as ((payload #>> '{counts,judge_admitted}')::integer) stored,
    judge_rejected_count integer generated always as ((payload #>> '{counts,judge_rejected}')::integer) stored,
    protection_override_count integer generated always as ((payload #>> '{counts,protection_overrides}')::integer) stored,
    rule_downgraded_count integer generated always as ((payload #>> '{counts,rule_downgraded}')::integer) stored,
    counts_by_rule jsonb generated always as (payload -> 'by_rule') stored,
    received_at timestamptz not null default now()
);

alter table public.ai_review_events enable row level security;
revoke all on public.ai_review_events from anon, authenticated;
grant select, insert, delete on public.ai_review_events to service_role;
