-- Research-only market comparison storage for v5 evaluation.
-- These tables are not inputs to production predictions or simulations.

create extension if not exists "pgcrypto";

create table if not exists public.market_odds_snapshots (
  id uuid primary key default gen_random_uuid(),
  match_id uuid references public.matches(id) on delete cascade,
  canonical_match_id text,
  provider_fixture_id bigint,
  provider_home_team_id bigint,
  provider_away_team_id bigint,
  provider_home_team_name text,
  provider_away_team_name text,
  canonical_home_team_code text,
  canonical_away_team_code text,
  bookmaker text not null,
  source text not null,
  collected_at timestamptz not null,
  home_decimal_odds double precision not null check (home_decimal_odds > 1),
  draw_decimal_odds double precision not null check (draw_decimal_odds > 1),
  away_decimal_odds double precision not null check (away_decimal_odds > 1),
  raw_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  check (match_id is not null or canonical_match_id is not null)
);

create table if not exists public.market_implied_probabilities (
  id uuid primary key default gen_random_uuid(),
  snapshot_id uuid not null references public.market_odds_snapshots(id)
    on delete cascade,
  match_id uuid references public.matches(id) on delete cascade,
  canonical_match_id text,
  model_run_id uuid references public.model_runs(id) on delete set null,
  model_version text,
  calculated_at timestamptz not null default now(),
  raw_home_probability double precision not null check (
    raw_home_probability between 0 and 1
  ),
  raw_draw_probability double precision not null check (
    raw_draw_probability between 0 and 1
  ),
  raw_away_probability double precision not null check (
    raw_away_probability between 0 and 1
  ),
  overround double precision not null check (overround > 0),
  devig_home_probability double precision not null check (
    devig_home_probability between 0 and 1
  ),
  devig_draw_probability double precision not null check (
    devig_draw_probability between 0 and 1
  ),
  devig_away_probability double precision not null check (
    devig_away_probability between 0 and 1
  ),
  model_home_probability double precision check (
    model_home_probability is null or model_home_probability between 0 and 1
  ),
  model_draw_probability double precision check (
    model_draw_probability is null or model_draw_probability between 0 and 1
  ),
  model_away_probability double precision check (
    model_away_probability is null or model_away_probability between 0 and 1
  ),
  home_probability_difference double precision,
  draw_probability_difference double precision,
  away_probability_difference double precision,
  average_absolute_disagreement double precision,
  disagreement_bucket text check (
    disagreement_bucket is null
    or disagreement_bucket in ('0-2%', '2-5%', '5-10%', '10%+')
  ),
  created_at timestamptz not null default now(),
  unique (snapshot_id, model_run_id)
);

create table if not exists public.market_comparison_reports (
  id uuid primary key default gen_random_uuid(),
  model_version text not null,
  generated_at timestamptz not null default now(),
  status text not null check (
    status in (
      'insufficient_coverage',
      'current_comparison_only',
      'historical_validation_complete'
    )
  ),
  snapshot_count integer not null default 0 check (snapshot_count >= 0),
  comparison_match_count integer not null default 0 check (
    comparison_match_count >= 0
  ),
  historical_match_count integer not null default 0 check (
    historical_match_count >= 0
  ),
  model_brier_score double precision,
  model_log_loss double precision,
  market_brier_score double precision,
  market_log_loss double precision,
  average_absolute_disagreement double precision,
  disagreement_buckets jsonb not null default '{}'::jsonb,
  calibration jsonb not null default '{}'::jsonb,
  coverage jsonb not null default '{}'::jsonb,
  report jsonb not null,
  created_at timestamptz not null default now()
);

create unique index if not exists market_odds_snapshot_identity_uidx
  on public.market_odds_snapshots (
    coalesce(match_id::text, canonical_match_id),
    bookmaker,
    collected_at
  );

create index if not exists market_odds_canonical_collected_idx
  on public.market_odds_snapshots (canonical_match_id, collected_at desc);

create index if not exists market_implied_canonical_calculated_idx
  on public.market_implied_probabilities (
    canonical_match_id,
    calculated_at desc
  );

create index if not exists market_comparison_reports_generated_idx
  on public.market_comparison_reports (generated_at desc);
