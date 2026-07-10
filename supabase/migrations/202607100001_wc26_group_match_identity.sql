-- Stable canonical identity for tournament fixtures. The UUID primary key stays
-- unchanged so existing foreign keys remain valid.
alter table public.matches
  add column if not exists canonical_match_id text,
  add column if not exists match_number integer;

create unique index if not exists matches_canonical_match_id_uidx
  on public.matches (canonical_match_id)
  where canonical_match_id is not null;

alter table public.matches drop constraint if exists matches_wc26_canonical_id_check;
alter table public.matches add constraint matches_wc26_canonical_id_check check (
  canonical_match_id is null or
  canonical_match_id ~ '^WC26-0(0[1-9]|[1-6][0-9]|7[0-2])$'
) not valid;

comment on column public.matches.canonical_match_id is
  'Stable tournament identifier such as WC26-001; distinct from the UUID row key.';
