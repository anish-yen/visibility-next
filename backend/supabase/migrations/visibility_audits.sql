-- Audit state is durable across API restarts and workers. Backend writes as service role.
create table if not exists visibility_audits (
  id uuid primary key,
  user_id uuid not null,
  primary_domain text not null,
  industry text,
  competitor_domains jsonb not null default '[]',
  status text not null,
  stage text not null,
  progress_percent int not null,
  visibility_score double precision,
  target_mention_rate double precision,
  competitor_scores jsonb not null default '[]',
  prompts jsonb not null default '[]',
  recommendations jsonb not null default '[]',
  crawl_summary jsonb not null default '{}',
  created_at timestamptz not null,
  error_message text
);
create index if not exists visibility_audits_owner_created_idx
  on visibility_audits (user_id, created_at desc);
alter table visibility_audits enable row level security;
-- No direct user policies: reads and writes go through the owner-checking backend.
