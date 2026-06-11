-- Research-only squad availability storage for v4.1 validation.
-- Nothing in the production prediction or simulation path reads these tables.

create extension if not exists "pgcrypto";

create table if not exists public.player_availability_reports (
  id uuid primary key default gen_random_uuid(),
  team_id uuid references public.teams(id) on delete cascade,
  team_code text,
  player_id uuid references public.players(id) on delete set null,
  provider_player_id bigint,
  player_name text not null,
  position text,
  status text not null check (
    status in ('available', 'injured', 'suspended', 'unknown')
  ),
  reason text,
  fixture_id uuid references public.matches(id) on delete cascade,
  provider_fixture_id bigint,
  canonical_home_team_code text,
  canonical_away_team_code text,
  expected_return timestamptz,
  source text not null,
  collected_at timestamptz not null default now(),
  raw_payload jsonb not null default '{}'::jsonb,
  check (team_id is not null or team_code is not null),
  check (player_id is not null or provider_player_id is not null)
);

create table if not exists public.projected_lineups (
  id uuid primary key default gen_random_uuid(),
  team_id uuid references public.teams(id) on delete cascade,
  team_code text,
  fixture_id uuid references public.matches(id) on delete cascade,
  provider_fixture_id bigint,
  canonical_home_team_code text,
  canonical_away_team_code text,
  player_id uuid references public.players(id) on delete set null,
  provider_player_id bigint,
  player_name text not null,
  position text,
  lineup_status text not null default 'projected' check (
    lineup_status in ('projected', 'confirmed', 'substitute', 'unknown')
  ),
  formation text,
  projected_minutes double precision check (
    projected_minutes is null or projected_minutes between 0 and 120
  ),
  player_strength double precision,
  source text not null,
  collected_at timestamptz not null default now(),
  raw_payload jsonb not null default '{}'::jsonb,
  check (team_id is not null or team_code is not null),
  check (player_id is not null or provider_player_id is not null)
);

create table if not exists public.squad_strength_ratings (
  id uuid primary key default gen_random_uuid(),
  team_id uuid references public.teams(id) on delete cascade,
  team_code text,
  fixture_id uuid references public.matches(id) on delete cascade,
  provider_fixture_id bigint,
  canonical_home_team_code text,
  canonical_away_team_code text,
  model_version text not null default 'squad-v4.1-research',
  squad_strength double precision,
  available_squad_strength double precision,
  projected_lineup_strength double precision,
  unavailable_player_penalty double precision not null default 0,
  depth_strength double precision,
  player_count integer not null default 0 check (player_count >= 0),
  available_player_count integer not null default 0 check (
    available_player_count >= 0
  ),
  lineup_player_count integer not null default 0 check (lineup_player_count >= 0),
  coverage_level double precision not null default 0 check (
    coverage_level between 0 and 1
  ),
  source text not null,
  rated_at timestamptz not null default now(),
  components jsonb not null default '{}'::jsonb,
  check (team_id is not null or team_code is not null)
);

alter table public.player_availability_reports
  add column if not exists canonical_home_team_code text,
  add column if not exists canonical_away_team_code text;

alter table public.projected_lineups
  add column if not exists canonical_home_team_code text,
  add column if not exists canonical_away_team_code text;

alter table public.squad_strength_ratings
  add column if not exists canonical_home_team_code text,
  add column if not exists canonical_away_team_code text;

create index if not exists player_availability_fixture_team_idx
  on public.player_availability_reports (
    fixture_id, team_id, collected_at desc
  );
create index if not exists player_availability_provider_fixture_idx
  on public.player_availability_reports (
    provider_fixture_id, provider_player_id, collected_at desc
  );
create index if not exists projected_lineups_fixture_team_idx
  on public.projected_lineups (fixture_id, team_id, collected_at desc);
create index if not exists squad_strength_fixture_team_idx
  on public.squad_strength_ratings (fixture_id, team_id, rated_at desc);
create index if not exists squad_strength_current_team_idx
  on public.squad_strength_ratings (team_id, rated_at desc);
