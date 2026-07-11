-- Append-only provenance for leakage-safe historical prediction backfills.

alter table public.predictions
  add column if not exists generation_mode text not null default 'standard',
  add column if not exists historical_cutoff timestamptz,
  add column if not exists backfilled_at timestamptz,
  add column if not exists maximum_source_timestamp timestamptz;

alter table public.predictions drop constraint if exists predictions_generation_mode_check;
alter table public.predictions add constraint predictions_generation_mode_check check (
  generation_mode in ('standard', 'historical_backfill')
);

alter table public.predictions drop constraint if exists predictions_historical_provenance_check;
alter table public.predictions add constraint predictions_historical_provenance_check check (
  generation_mode <> 'historical_backfill'
  or (historical_cutoff is not null and backfilled_at is not null)
);

-- Predictions are versioned by model_run_id.  These older indexes enforced a
-- mutable "current row" and prevented an authentic row and a backfill from
-- coexisting for the same fixture.
drop index if exists public.predictions_match_uidx;
drop index if exists public.predictions_canonical_match_uidx;

create index if not exists predictions_match_history_idx
  on public.predictions (match_id, prediction_timestamp desc);
create index if not exists predictions_canonical_history_idx
  on public.predictions (canonical_match_id, prediction_timestamp desc)
  where canonical_match_id is not null;
create unique index if not exists predictions_historical_backfill_match_uidx
  on public.predictions (match_id, model_version)
  where generation_mode = 'historical_backfill' and match_id is not null;

comment on column public.predictions.historical_cutoff is
  'Exclusive source-data cutoff used to reconstruct historical model inputs.';
comment on column public.predictions.backfilled_at is
  'Actual execution time; never represented as a pre-kickoff publication time.';
