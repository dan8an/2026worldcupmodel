-- Allow predictions for canonical fixtures that do not yet exist in the
-- provider-ingested matches table.

alter table public.predictions
  add column if not exists canonical_match_id text;

alter table public.predictions
  alter column match_id drop not null;

update public.predictions
set canonical_match_id = match_id::text
where canonical_match_id is null;

with ranked as (
  select
    id,
    row_number() over (
      partition by canonical_match_id
      order by
        prediction_timestamp desc nulls last,
        created_at desc nulls last,
        id desc
    ) as row_number
  from public.predictions
  where canonical_match_id is not null
)
delete from public.predictions
where id in (select id from ranked where row_number > 1);

create unique index if not exists predictions_canonical_match_uidx
  on public.predictions (canonical_match_id)
  where canonical_match_id is not null;