-- Step 4: fields used by the deterministic Poisson prediction generator.

alter table public.predictions
  add column if not exists prediction_timestamp timestamptz,
  add column if not exists model_version text,
  add column if not exists confidence_score double precision,
  add column if not exists home_win_probability double precision,
  add column if not exists draw_probability double precision,
  add column if not exists away_win_probability double precision,
  add column if not exists most_likely_scoreline text,
  add column if not exists expected_total_goals double precision,
  add column if not exists over_2_5_probability double precision,
  add column if not exists both_teams_to_score_probability double precision,
  add column if not exists score_probabilities jsonb not null default '[]'::jsonb;

-- The daily pipeline exposes one current prediction per match. Model-run rows
-- retain the history of when each generation occurred.
with ranked as (
  select
    id,
    row_number() over (
      partition by match_id
      order by
        prediction_timestamp desc nulls last,
        created_at desc nulls last,
        id desc
    ) as row_number
  from public.predictions
)
delete from public.predictions
where id in (select id from ranked where row_number > 1);

create unique index if not exists predictions_match_uidx
  on public.predictions (match_id);
