-- Step 3: additive columns and uniqueness for current team/player ratings.
--
-- Rows with model_run_id is null are the latest reusable ratings produced by
-- scripts/update_ratings.py. Rows tied to a model run remain available for
-- future immutable prediction snapshots.

alter table public.team_ratings
  add column if not exists matches_played integer,
  add column if not exists goals_for integer,
  add column if not exists goals_against integer,
  add column if not exists updated_at timestamptz not null default now();

alter table public.player_ratings
  add column if not exists goal_threat double precision,
  add column if not exists assist_threat double precision,
  add column if not exists shot_volume double precision,
  add column if not exists minutes_rating double precision,
  add column if not exists form_rating double precision,
  add column if not exists matches_played integer,
  add column if not exists minutes_played integer,
  add column if not exists updated_at timestamptz not null default now();

-- Keep the newest current row if an earlier manual run created duplicates.
with ranked as (
  select
    id,
    row_number() over (
      partition by team_id
      order by rated_at desc nulls last, created_at desc nulls last, id desc
    ) as row_number
  from public.team_ratings
  where model_run_id is null
)
delete from public.team_ratings
where id in (select id from ranked where row_number > 1);

with ranked as (
  select
    id,
    row_number() over (
      partition by player_id
      order by rated_at desc nulls last, created_at desc nulls last, id desc
    ) as row_number
  from public.player_ratings
  where model_run_id is null
)
delete from public.player_ratings
where id in (select id from ranked where row_number > 1);

create unique index if not exists team_ratings_current_team_uidx
  on public.team_ratings (team_id)
  where model_run_id is null;

create unique index if not exists player_ratings_current_player_uidx
  on public.player_ratings (player_id)
  where model_run_id is null;
