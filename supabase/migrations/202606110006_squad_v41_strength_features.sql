-- Interpretable research-only squad depth and availability features.
-- Production prediction and simulation paths do not read this table.

alter table public.squad_strength_ratings
  add column if not exists squad_size integer not null default 0
    check (squad_size >= 0),
  add column if not exists available_players integer not null default 0
    check (available_players >= 0),
  add column if not exists unavailable_players integer not null default 0
    check (unavailable_players >= 0),
  add column if not exists known_position_counts integer not null default 0
    check (known_position_counts >= 0),
  add column if not exists goalkeeper_count integer not null default 0
    check (goalkeeper_count >= 0),
  add column if not exists defender_count integer not null default 0
    check (defender_count >= 0),
  add column if not exists midfielder_count integer not null default 0
    check (midfielder_count >= 0),
  add column if not exists attacker_count integer not null default 0
    check (attacker_count >= 0),
  add column if not exists squad_depth_score double precision
    check (squad_depth_score is null or squad_depth_score between 0 and 100),
  add column if not exists availability_score double precision
    check (availability_score is null or availability_score between 0 and 100),
  add column if not exists data_completeness_score double precision
    check (
      data_completeness_score is null
      or data_completeness_score between 0 and 100
    ),
  add column if not exists rating_source text not null default 'squad_depth_only';

create index if not exists squad_strength_provider_fixture_team_idx
  on public.squad_strength_ratings (
    provider_fixture_id,
    team_code,
    rated_at desc
  );
