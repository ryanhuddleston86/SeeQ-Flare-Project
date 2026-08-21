-- Workout Accountability Tracker — Supabase schema.
-- Run this once in the Supabase SQL editor (Dashboard > SQL Editor > New query).

create extension if not exists "pgcrypto";

create table if not exists public.workout_logs (
  id         uuid primary key default gen_random_uuid(),
  user_name  text not null check (user_name in ('Ryan', 'Clay', 'John')),
  log_date   date not null,
  track      text not null check (track in ('gym', 'home')),
  day        text not null check (day in ('Day 1', 'Day 2', 'Day 3', 'Optional Day 4')),
  note       text,
  created_at timestamptz not null default now()
);

-- One person can log each day-slot once per date. Keeps a double-tap on the
-- submit button from inflating the leaderboard.
create unique index if not exists workout_logs_unique_slot
  on public.workout_logs (user_name, log_date, day);

-- The weekly view and leaderboard both scan by date.
create index if not exists workout_logs_log_date_idx
  on public.workout_logs (log_date desc);

-- Row-level security. The anon key ships in the built JS bundle, so these
-- policies are the only thing standing between the public and the table.
alter table public.workout_logs enable row level security;

drop policy if exists "known names can read" on public.workout_logs;
create policy "known names can read"
  on public.workout_logs
  for select
  to anon, authenticated
  using (user_name in ('Ryan', 'Clay', 'John'));

drop policy if exists "known names can insert" on public.workout_logs;
create policy "known names can insert"
  on public.workout_logs
  for insert
  to anon, authenticated
  with check (user_name in ('Ryan', 'Clay', 'John'));

-- No update or delete policy exists, so the anon key cannot change or remove
-- rows. Fix a bad entry from the Supabase dashboard (which uses the service
-- role and bypasses RLS).
