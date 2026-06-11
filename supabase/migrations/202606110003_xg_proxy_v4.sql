-- Experimental xg-proxy-v4 storage. This does not alter production predictions.

alter table public.team_match_stats
  add column if not exists shots_inside_box integer,
  add column if not exists shots_outside_box integer,
  add column if not exists blocked_shots integer,
  add column if not exists goalkeeper_saves integer,
  add column if not exists pass_accuracy double precision;

create table if not exists public.team_chance_quality_ratings (
  id uuid primary key default gen_random_uuid(),
  team_id uuid not null references public.teams(id),
  rated_at timestamptz not null,
  model_version text not null default 'xg-proxy-v4',
  sample_matches integer not null,
  shot_volume_rating double precision,
  shot_quality_proxy double precision,
  box_shot_rate double precision,
  shots_on_target_rate double precision,
  chance_creation_rating double precision,
  defensive_shot_suppression double precision,
  keeper_pressure_allowed double precision,
  components jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create unique index if not exists team_chance_quality_current_uidx
  on public.team_chance_quality_ratings (team_id, model_version);

create index if not exists team_chance_quality_rated_at_idx
  on public.team_chance_quality_ratings (rated_at desc);
