create extension if not exists "pgcrypto";

create table teams (
  id text primary key,
  name text not null,
  group_code char(1) not null,
  group_position smallint not null check (group_position between 1 and 4),
  fifa_rank integer,
  is_host boolean not null default false,
  created_at timestamptz not null default now()
);

create table venues (
  id text primary key,
  name text not null,
  city text not null,
  country text not null,
  timezone text not null,
  latitude double precision not null,
  longitude double precision not null
);

create table matches (
  id text primary key,
  match_number integer not null unique,
  stage text not null,
  group_code char(1),
  kickoff timestamptz not null,
  venue_id text not null references venues(id),
  home_team_id text references teams(id),
  away_team_id text references teams(id),
  home_slot text,
  away_slot text,
  status text not null default 'scheduled',
  created_at timestamptz not null default now(),
  check (home_team_id is null or away_team_id is null or home_team_id <> away_team_id)
);

create table model_versions (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  semantic_version text not null unique,
  git_sha text,
  training_cutoff timestamptz,
  feature_schema_version text not null,
  metrics jsonb not null default '{}'::jsonb,
  artifact_uri text,
  created_at timestamptz not null default now()
);

create table prediction_runs (
  id uuid primary key default gen_random_uuid(),
  model_version_id uuid not null references model_versions(id),
  data_cutoff timestamptz not null,
  random_seed integer not null,
  status text not null,
  generated_at timestamptz not null default now()
);

create table match_predictions (
  id uuid primary key default gen_random_uuid(),
  match_id text not null references matches(id),
  prediction_run_id uuid not null references prediction_runs(id),
  home_win double precision not null check (home_win between 0 and 1),
  draw double precision not null check (draw between 0 and 1),
  away_win double precision not null check (away_win between 0 and 1),
  home_xg double precision not null check (home_xg >= 0),
  away_xg double precision not null check (away_xg >= 0),
  confidence_tier text not null,
  explanation_factors jsonb not null default '[]'::jsonb,
  unique (match_id, prediction_run_id),
  check (abs(home_win + draw + away_win - 1.0) < 0.00001)
);

create table scoreline_predictions (
  match_prediction_id uuid not null references match_predictions(id) on delete cascade,
  home_goals smallint not null check (home_goals >= 0),
  away_goals smallint not null check (away_goals >= 0),
  probability double precision not null check (probability between 0 and 1),
  primary key (match_prediction_id, home_goals, away_goals)
);

create table simulation_runs (
  id uuid primary key default gen_random_uuid(),
  prediction_run_id uuid not null references prediction_runs(id),
  iterations integer not null check (iterations > 0),
  random_seed integer not null,
  created_at timestamptz not null default now()
);

create table team_tournament_probabilities (
  simulation_run_id uuid not null references simulation_runs(id) on delete cascade,
  team_id text not null references teams(id),
  round_of_32 double precision not null,
  round_of_16 double precision not null,
  quarterfinal double precision not null,
  semifinal double precision not null,
  final double precision not null,
  champion double precision not null,
  primary key (simulation_run_id, team_id)
);

create table saved_simulations (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  scenario jsonb not null,
  result jsonb not null,
  created_at timestamptz not null default now()
);

alter table saved_simulations enable row level security;
create policy "Users can read their simulations"
  on saved_simulations for select using (auth.uid() = user_id);
create policy "Users can create their simulations"
  on saved_simulations for insert with check (auth.uid() = user_id);

