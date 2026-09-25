-- Supabase migration: persistent cycle snapshots for the agentic visibility loop.
-- Run in the Supabase SQL editor. Without this, audit_store.py keeps everything
-- in memory and every audit (and every before/after comparison) dies on restart.

create table if not exists visibility_cycles (
  id uuid primary key default gen_random_uuid(),
  audit_id uuid not null,
  user_id uuid not null,
  cycle_number int not null,
  mention_rate numeric,
  citation_map jsonb,
  prompt_results jsonb,
  tech_checks jsonb,
  artifacts jsonb,
  lift numeric,
  decision text,
  decision_reason text,
  created_at timestamptz not null default now(),
  unique (audit_id, cycle_number)
);

create index if not exists visibility_cycles_audit_id_idx
  on visibility_cycles (audit_id, cycle_number desc);

alter table visibility_cycles enable row level security;

-- Users read only their own cycles. Writes go through the service role (backend),
-- which bypasses RLS.
create policy "users read own cycles"
  on visibility_cycles for select
  using (auth.uid() = user_id);
