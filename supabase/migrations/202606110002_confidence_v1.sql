-- confidence-v1 stores a validated 0-100 reliability score and explanation.

alter table public.predictions
  add column if not exists confidence_explanation text;

comment on column public.predictions.confidence_score is
  'confidence-v1 reliability score from 0 to 100; not an outcome probability.';
comment on column public.predictions.confidence_tier is
  'Validated confidence-v1 tier: High, Medium, or Low.';
comment on column public.predictions.confidence_explanation is
  'Plain-language explanation of the confidence-v1 score.';
