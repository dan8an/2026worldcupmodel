-- Persist both provider and canonical team orientation for market snapshots.
-- Evaluation rejects snapshots whose home/away identity does not match the
-- canonical fixture.

alter table public.market_odds_snapshots
  add column if not exists provider_home_team_id bigint,
  add column if not exists provider_away_team_id bigint,
  add column if not exists provider_home_team_name text,
  add column if not exists provider_away_team_name text,
  add column if not exists canonical_home_team_code text,
  add column if not exists canonical_away_team_code text;

create index if not exists market_odds_fixture_orientation_idx
  on public.market_odds_snapshots (
    provider_fixture_id,
    canonical_home_team_code,
    canonical_away_team_code
  );
