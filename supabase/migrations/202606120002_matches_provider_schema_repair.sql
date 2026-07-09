-- Repair provider-ingestion columns for deployed schemas that predate the
-- completed knockout fixture upsert.
--
-- This migration is intentionally idempotent: it can be applied to production
-- Supabase databases that already ran the broader daily-pipeline migration, as
-- well as leaner databases that only need provider fixture identity columns.

alter table public.matches
  add column if not exists status text,
  add column if not exists api_football_fixture_id bigint,
  add column if not exists provider_name text,
  add column if not exists provider_payload jsonb;

create unique index if not exists matches_api_football_fixture_id_uidx
  on public.matches (api_football_fixture_id)
  where api_football_fixture_id is not null;
