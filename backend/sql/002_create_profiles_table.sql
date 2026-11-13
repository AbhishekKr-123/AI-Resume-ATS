-- Run manually in the Supabase SQL Editor after 001_create_analyses_table.sql.
-- Email remains authoritative in auth.users and is intentionally not duplicated here.
create table if not exists public.profiles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  full_name text not null check (length(btrim(full_name)) > 0 and length(full_name) <= 200),
  linkedin_url text check (linkedin_url is null or linkedin_url ~* '^https?://[^[:space:]]+$'),
  github_url text check (github_url is null or github_url ~* '^https?://[^[:space:]]+$'),
  leetcode_url text check (leetcode_url is null or leetcode_url ~* '^https?://[^[:space:]]+$'),
  portfolio_url text check (portfolio_url is null or portfolio_url ~* '^https?://[^[:space:]]+$'),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create or replace function public.set_profile_updated_at()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists profiles_set_updated_at on public.profiles;
create trigger profiles_set_updated_at
before update on public.profiles
for each row execute function public.set_profile_updated_at();

alter table public.profiles enable row level security;

drop policy if exists "profiles_select_own" on public.profiles;
drop policy if exists "profiles_insert_own" on public.profiles;
drop policy if exists "profiles_update_own" on public.profiles;

create policy "profiles_select_own"
on public.profiles for select
to authenticated
using (user_id = auth.uid());

create policy "profiles_insert_own"
on public.profiles for insert
to authenticated
with check (user_id = auth.uid());

create policy "profiles_update_own"
on public.profiles for update
to authenticated
using (user_id = auth.uid())
with check (user_id = auth.uid());

revoke all on table public.profiles from anon;
grant select, insert, update on table public.profiles to authenticated;
