-- Daily prediction pipeline storage.
--
-- Assumptions verified against the live Supabase schema on 2026-06-10:
--   public.teams(id uuid)
--   public.matches(id uuid)
--   public.model_runs(id uuid)
--   public.predictions(id uuid)
--
-- Existing tables are extended only with nullable/defaulted columns. No rows
-- are rewritten or deleted, and the existing text-based match team columns
-- remain in place for backward compatibility with current API routes.

create extension if not exists "pgcrypto";

alter table public.teams
  add column if not exists api_football_team_id bigint;

alter table public.matches
  add column if not exists home_team_id uuid,
  add column if not exists away_team_id uuid,
  add column if not exists api_football_fixture_id bigint,
  add column if not exists provider_name text,
  add column if not exists provider_payload jsonb not null default '{}'::jsonb,
  add column if not exists updated_at timestamptz not null default now();

alter table public.model_runs
  add column if not exists data_cutoff timestamptz,
  add column if not exists status text not null default 'pending',
  add column if not exists random_seed integer,
  add column if not exists generated_at timestamptz not null default now(),
  add column if not exists metadata jsonb not null default '{}'::jsonb;

alter table public.predictions
  add column if not exists model_run_id uuid,
  add column if not exists home_xg double precision,
  add column if not exists away_xg double precision,
  add column if not exists confidence_tier text,
  add column if not exists explanation_factors jsonb not null default '[]'::jsonb,
  add column if not exists data_cutoff timestamptz;

create table if not exists public.players (
  id uuid primary key default gen_random_uuid(),
  team_id uuid,
  provider_key text,
  display_name text not null,
  date_of_birth date,
  primary_position text,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.players
  add column if not exists team_id uuid,
  add column if not exists provider_key text,
  add column if not exists display_name text,
  add column if not exists date_of_birth date,
  add column if not exists primary_position text,
  add column if not exists active boolean not null default true,
  add column if not exists created_at timestamptz not null default now(),
  add column if not exists updated_at timestamptz not null default now();

create table if not exists public.team_match_stats (
  id uuid primary key default gen_random_uuid(),
  match_id uuid not null,
  team_id uuid not null,
  opponent_team_id uuid,
  is_home boolean,
  goals integer,
  expected_goals double precision,
  possession double precision,
  shots integer,
  shots_on_target integer,
  corners integer,
  fouls integer,
  yellow_cards integer,
  red_cards integer,
  passes_attempted integer,
  passes_completed integer,
  ppda double precision,
  source_name text,
  source_match_key text,
  captured_at timestamptz not null default now(),
  raw_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

alter table public.team_match_stats
  add column if not exists match_id uuid,
  add column if not exists team_id uuid,
  add column if not exists opponent_team_id uuid,
  add column if not exists is_home boolean,
  add column if not exists goals integer,
  add column if not exists expected_goals double precision,
  add column if not exists possession double precision,
  add column if not exists shots integer,
  add column if not exists shots_on_target integer,
  add column if not exists corners integer,
  add column if not exists fouls integer,
  add column if not exists yellow_cards integer,
  add column if not exists red_cards integer,
  add column if not exists passes_attempted integer,
  add column if not exists passes_completed integer,
  add column if not exists ppda double precision,
  add column if not exists source_name text,
  add column if not exists source_match_key text,
  add column if not exists captured_at timestamptz not null default now(),
  add column if not exists raw_payload jsonb not null default '{}'::jsonb,
  add column if not exists created_at timestamptz not null default now();

create table if not exists public.player_match_stats (
  id uuid primary key default gen_random_uuid(),
  match_id uuid not null,
  player_id uuid not null,
  team_id uuid not null,
  opponent_team_id uuid,
  started boolean,
  minutes_played integer,
  goals integer,
  assists integer,
  expected_goals double precision,
  expected_assists double precision,
  shots integer,
  shots_on_target integer,
  key_passes integer,
  tackles integer,
  interceptions integer,
  saves integer,
  yellow_cards integer,
  red_cards integer,
  source_name text,
  source_player_key text,
  captured_at timestamptz not null default now(),
  raw_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

alter table public.player_match_stats
  add column if not exists match_id uuid,
  add column if not exists player_id uuid,
  add column if not exists team_id uuid,
  add column if not exists opponent_team_id uuid,
  add column if not exists started boolean,
  add column if not exists minutes_played integer,
  add column if not exists goals integer,
  add column if not exists assists integer,
  add column if not exists expected_goals double precision,
  add column if not exists expected_assists double precision,
  add column if not exists shots integer,
  add column if not exists shots_on_target integer,
  add column if not exists key_passes integer,
  add column if not exists tackles integer,
  add column if not exists interceptions integer,
  add column if not exists saves integer,
  add column if not exists yellow_cards integer,
  add column if not exists red_cards integer,
  add column if not exists source_name text,
  add column if not exists source_player_key text,
  add column if not exists captured_at timestamptz not null default now(),
  add column if not exists raw_payload jsonb not null default '{}'::jsonb,
  add column if not exists created_at timestamptz not null default now();

create table if not exists public.team_ratings (
  id uuid primary key default gen_random_uuid(),
  team_id uuid not null,
  model_run_id uuid,
  rated_at timestamptz not null,
  elo_rating double precision,
  attack_rating double precision,
  defense_rating double precision,
  form_rating double precision,
  fifa_rank integer,
  sample_matches integer,
  components jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

alter table public.team_ratings
  add column if not exists team_id uuid,
  add column if not exists model_run_id uuid,
  add column if not exists rated_at timestamptz,
  add column if not exists elo_rating double precision,
  add column if not exists attack_rating double precision,
  add column if not exists defense_rating double precision,
  add column if not exists form_rating double precision,
  add column if not exists fifa_rank integer,
  add column if not exists sample_matches integer,
  add column if not exists components jsonb not null default '{}'::jsonb,
  add column if not exists created_at timestamptz not null default now();

create table if not exists public.player_ratings (
  id uuid primary key default gen_random_uuid(),
  player_id uuid not null,
  team_id uuid,
  model_run_id uuid,
  rated_at timestamptz not null,
  overall_rating double precision,
  attacking_rating double precision,
  creative_rating double precision,
  defensive_rating double precision,
  goalkeeping_rating double precision,
  availability_rating double precision,
  projected_minutes double precision,
  components jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

alter table public.player_ratings
  add column if not exists player_id uuid,
  add column if not exists team_id uuid,
  add column if not exists model_run_id uuid,
  add column if not exists rated_at timestamptz,
  add column if not exists overall_rating double precision,
  add column if not exists attacking_rating double precision,
  add column if not exists creative_rating double precision,
  add column if not exists defensive_rating double precision,
  add column if not exists goalkeeping_rating double precision,
  add column if not exists availability_rating double precision,
  add column if not exists projected_minutes double precision,
  add column if not exists components jsonb not null default '{}'::jsonb,
  add column if not exists created_at timestamptz not null default now();

create table if not exists public.odds_snapshots (
  id uuid primary key default gen_random_uuid(),
  match_id uuid not null,
  model_run_id uuid,
  bookmaker text not null,
  market text not null,
  selection text not null,
  line double precision,
  decimal_odds double precision not null,
  implied_probability double precision,
  captured_at timestamptz not null,
  source_url text,
  raw_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

alter table public.odds_snapshots
  add column if not exists match_id uuid,
  add column if not exists model_run_id uuid,
  add column if not exists bookmaker text,
  add column if not exists market text,
  add column if not exists selection text,
  add column if not exists line double precision,
  add column if not exists decimal_odds double precision,
  add column if not exists implied_probability double precision,
  add column if not exists captured_at timestamptz,
  add column if not exists source_url text,
  add column if not exists raw_payload jsonb not null default '{}'::jsonb,
  add column if not exists created_at timestamptz not null default now();

do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'matches_home_team_id_fkey'
      and conrelid = 'public.matches'::regclass
  ) then
    alter table public.matches
      add constraint matches_home_team_id_fkey
      foreign key (home_team_id) references public.teams(id);
  end if;

  if not exists (
    select 1 from pg_constraint
    where conname = 'matches_away_team_id_fkey'
      and conrelid = 'public.matches'::regclass
  ) then
    alter table public.matches
      add constraint matches_away_team_id_fkey
      foreign key (away_team_id) references public.teams(id);
  end if;

  if not exists (
    select 1 from pg_constraint
    where conname = 'predictions_model_run_id_fkey'
      and conrelid = 'public.predictions'::regclass
  ) then
    alter table public.predictions
      add constraint predictions_model_run_id_fkey
      foreign key (model_run_id) references public.model_runs(id);
  end if;

  if not exists (
    select 1 from pg_constraint
    where conname = 'players_team_id_fkey'
      and conrelid = 'public.players'::regclass
  ) then
    alter table public.players
      add constraint players_team_id_fkey
      foreign key (team_id) references public.teams(id);
  end if;

  if not exists (
    select 1 from pg_constraint
    where conname = 'team_match_stats_match_id_fkey'
      and conrelid = 'public.team_match_stats'::regclass
  ) then
    alter table public.team_match_stats
      add constraint team_match_stats_match_id_fkey
      foreign key (match_id) references public.matches(id) on delete cascade;
  end if;

  if not exists (
    select 1 from pg_constraint
    where conname = 'team_match_stats_team_id_fkey'
      and conrelid = 'public.team_match_stats'::regclass
  ) then
    alter table public.team_match_stats
      add constraint team_match_stats_team_id_fkey
      foreign key (team_id) references public.teams(id);
  end if;

  if not exists (
    select 1 from pg_constraint
    where conname = 'team_match_stats_opponent_team_id_fkey'
      and conrelid = 'public.team_match_stats'::regclass
  ) then
    alter table public.team_match_stats
      add constraint team_match_stats_opponent_team_id_fkey
      foreign key (opponent_team_id) references public.teams(id);
  end if;

  if not exists (
    select 1 from pg_constraint
    where conname = 'player_match_stats_match_id_fkey'
      and conrelid = 'public.player_match_stats'::regclass
  ) then
    alter table public.player_match_stats
      add constraint player_match_stats_match_id_fkey
      foreign key (match_id) references public.matches(id) on delete cascade;
  end if;

  if not exists (
    select 1 from pg_constraint
    where conname = 'player_match_stats_player_id_fkey'
      and conrelid = 'public.player_match_stats'::regclass
  ) then
    alter table public.player_match_stats
      add constraint player_match_stats_player_id_fkey
      foreign key (player_id) references public.players(id);
  end if;

  if not exists (
    select 1 from pg_constraint
    where conname = 'player_match_stats_team_id_fkey'
      and conrelid = 'public.player_match_stats'::regclass
  ) then
    alter table public.player_match_stats
      add constraint player_match_stats_team_id_fkey
      foreign key (team_id) references public.teams(id);
  end if;

  if not exists (
    select 1 from pg_constraint
    where conname = 'player_match_stats_opponent_team_id_fkey'
      and conrelid = 'public.player_match_stats'::regclass
  ) then
    alter table public.player_match_stats
      add constraint player_match_stats_opponent_team_id_fkey
      foreign key (opponent_team_id) references public.teams(id);
  end if;

  if not exists (
    select 1 from pg_constraint
    where conname = 'team_ratings_team_id_fkey'
      and conrelid = 'public.team_ratings'::regclass
  ) then
    alter table public.team_ratings
      add constraint team_ratings_team_id_fkey
      foreign key (team_id) references public.teams(id);
  end if;

  if not exists (
    select 1 from pg_constraint
    where conname = 'team_ratings_model_run_id_fkey'
      and conrelid = 'public.team_ratings'::regclass
  ) then
    alter table public.team_ratings
      add constraint team_ratings_model_run_id_fkey
      foreign key (model_run_id) references public.model_runs(id) on delete set null;
  end if;

  if not exists (
    select 1 from pg_constraint
    where conname = 'player_ratings_player_id_fkey'
      and conrelid = 'public.player_ratings'::regclass
  ) then
    alter table public.player_ratings
      add constraint player_ratings_player_id_fkey
      foreign key (player_id) references public.players(id);
  end if;

  if not exists (
    select 1 from pg_constraint
    where conname = 'player_ratings_team_id_fkey'
      and conrelid = 'public.player_ratings'::regclass
  ) then
    alter table public.player_ratings
      add constraint player_ratings_team_id_fkey
      foreign key (team_id) references public.teams(id);
  end if;

  if not exists (
    select 1 from pg_constraint
    where conname = 'player_ratings_model_run_id_fkey'
      and conrelid = 'public.player_ratings'::regclass
  ) then
    alter table public.player_ratings
      add constraint player_ratings_model_run_id_fkey
      foreign key (model_run_id) references public.model_runs(id) on delete set null;
  end if;

  if not exists (
    select 1 from pg_constraint
    where conname = 'odds_snapshots_match_id_fkey'
      and conrelid = 'public.odds_snapshots'::regclass
  ) then
    alter table public.odds_snapshots
      add constraint odds_snapshots_match_id_fkey
      foreign key (match_id) references public.matches(id) on delete cascade;
  end if;

  if not exists (
    select 1 from pg_constraint
    where conname = 'odds_snapshots_model_run_id_fkey'
      and conrelid = 'public.odds_snapshots'::regclass
  ) then
    alter table public.odds_snapshots
      add constraint odds_snapshots_model_run_id_fkey
      foreign key (model_run_id) references public.model_runs(id) on delete set null;
  end if;
end
$$;

create unique index if not exists players_provider_key_uidx
  on public.players (provider_key)
  where provider_key is not null;

create unique index if not exists teams_api_football_team_id_uidx
  on public.teams (api_football_team_id)
  where api_football_team_id is not null;

create unique index if not exists matches_api_football_fixture_id_uidx
  on public.matches (api_football_fixture_id)
  where api_football_fixture_id is not null;

create unique index if not exists team_match_stats_match_team_uidx
  on public.team_match_stats (match_id, team_id);

create unique index if not exists player_match_stats_match_player_team_uidx
  on public.player_match_stats (match_id, player_id, team_id);

create unique index if not exists team_ratings_run_team_uidx
  on public.team_ratings (model_run_id, team_id)
  where model_run_id is not null;

create index if not exists team_ratings_team_rated_at_idx
  on public.team_ratings (team_id, rated_at desc);

create unique index if not exists player_ratings_run_player_uidx
  on public.player_ratings (model_run_id, player_id)
  where model_run_id is not null;

create index if not exists player_ratings_player_rated_at_idx
  on public.player_ratings (player_id, rated_at desc);

create unique index if not exists odds_snapshots_market_uidx
  on public.odds_snapshots (
    match_id,
    bookmaker,
    market,
    selection,
    coalesce(line, '-Infinity'::double precision),
    captured_at
  );

create index if not exists odds_snapshots_match_captured_at_idx
  on public.odds_snapshots (match_id, captured_at desc);

create index if not exists predictions_model_run_id_idx
  on public.predictions (model_run_id);

create unique index if not exists predictions_match_model_run_uidx
  on public.predictions (match_id, model_run_id)
  where model_run_id is not null;
