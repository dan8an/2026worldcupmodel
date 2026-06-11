-- Allow research rows to identify provider-only fixtures without requiring a
-- public.matches row. The existing fixture_id columns remain nullable.

alter table public.player_availability_reports
  add column if not exists canonical_home_team_code text,
  add column if not exists canonical_away_team_code text;

alter table public.projected_lineups
  add column if not exists canonical_home_team_code text,
  add column if not exists canonical_away_team_code text;

alter table public.squad_strength_ratings
  add column if not exists canonical_home_team_code text,
  add column if not exists canonical_away_team_code text;

create index if not exists player_availability_provider_fixture_teams_idx
  on public.player_availability_reports (
    provider_fixture_id,
    canonical_home_team_code,
    canonical_away_team_code,
    collected_at desc
  );

create index if not exists projected_lineups_provider_fixture_teams_idx
  on public.projected_lineups (
    provider_fixture_id,
    canonical_home_team_code,
    canonical_away_team_code,
    collected_at desc
  );
