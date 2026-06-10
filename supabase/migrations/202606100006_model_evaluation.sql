-- Immutable model evaluation and backtesting results.

create extension if not exists "pgcrypto";

create table if not exists public.evaluation_results (
  id uuid primary key default gen_random_uuid(),
  model_version text not null,
  evaluated_at timestamptz not null default now(),
  evaluation_start date not null,
  evaluation_end date not null,
  match_count integer not null check (match_count > 0),
  brier_score double precision not null,
  log_loss double precision not null,
  accuracy double precision not null,
  elo_brier_score double precision not null,
  elo_log_loss double precision not null,
  elo_accuracy double precision not null,
  market_match_count integer not null default 0 check (market_match_count >= 0),
  market_brier_score double precision,
  market_log_loss double precision,
  market_accuracy double precision,
  calibration jsonb not null default '[]'::jsonb,
  confidence_tiers jsonb not null default '[]'::jsonb,
  report jsonb not null,
  protocol jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  check (evaluation_end >= evaluation_start),
  check (
    (market_match_count = 0 and market_brier_score is null
      and market_log_loss is null and market_accuracy is null)
    or
    (market_match_count > 0 and market_brier_score is not null
      and market_log_loss is not null and market_accuracy is not null)
  )
);

create index if not exists evaluation_results_model_evaluated_idx
  on public.evaluation_results (model_version, evaluated_at desc);
