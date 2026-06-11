-- Persist elo-context-v3 contribution tracking without rewriting prior rows.
-- Adjustment fields are signed probability-point shifts:
--   attack_defense_adjustment: home-win shift from attack/defense context
--   context_adjustment_total: home-win shift from all enabled context
--   draw_calibration_adjustment: draw shift from the draw multiplier

alter table public.predictions
  add column if not exists elo_base_home_probability double precision,
  add column if not exists elo_base_draw_probability double precision,
  add column if not exists elo_base_away_probability double precision,
  add column if not exists attack_defense_adjustment double precision,
  add column if not exists draw_calibration_adjustment double precision,
  add column if not exists context_adjustment_total double precision,
  add column if not exists final_home_probability double precision,
  add column if not exists final_draw_probability double precision,
  add column if not exists final_away_probability double precision,
  add column if not exists top_factors jsonb not null default '[]'::jsonb;

comment on column public.predictions.attack_defense_adjustment is
  'Signed home-win probability shift from the v3 attack/defense tilt.';
comment on column public.predictions.context_adjustment_total is
  'Signed home-win probability shift from all enabled v3 context before draw calibration.';
comment on column public.predictions.draw_calibration_adjustment is
  'Signed draw-probability shift from v3 draw calibration.';
