-- Run manually in the Supabase SQL Editor. This migration stores analysis
-- results only; it never stores original documents, extracted text, JD text,
-- contact information, credentials, or authentication tokens.
create table if not exists public.analyses (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  overall_score numeric(5,2) not null check (overall_score >= 0 and overall_score <= 100),
  component_scores jsonb not null,
  matched_skills jsonb not null default '[]'::jsonb,
  missing_skills jsonb not null default '[]'::jsonb,
  matched_keywords jsonb not null default '[]'::jsonb,
  missing_keywords jsonb not null default '[]'::jsonb,
  feedback jsonb,
  filename text,
  created_at timestamptz not null default now()
);

create index if not exists analyses_user_id_idx on public.analyses (user_id);
create index if not exists analyses_user_created_at_idx on public.analyses (user_id, created_at desc);

alter table public.analyses enable row level security;

drop policy if exists "analyses_select_own" on public.analyses;
drop policy if exists "analyses_insert_own" on public.analyses;
drop policy if exists "analyses_delete_own" on public.analyses;
create policy "analyses_select_own" on public.analyses for select to authenticated using (user_id = auth.uid());
create policy "analyses_insert_own" on public.analyses for insert to authenticated with check (user_id = auth.uid());
create policy "analyses_delete_own" on public.analyses for delete to authenticated using (user_id = auth.uid());
