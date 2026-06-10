-- Step 5: persisted Monte Carlo tournament simulations.
--
-- simulation_runs existed in an earlier schema, so extend it in place rather
-- than creating a competing run table.

create table if not exists public.simulation_runs (
  id uuid primary key default gen_random_uuid(),
  model_run_id uuid,
  model_version text,
  num_simulations integer,
  random_seed integer,
  created_at timestamptz not null default now()
);

alter table public.simulation_runs
  add column if not exists model_run_id uuid,
  add column if not exists model_version text,
  add column if not exists num_simulations integer,
  add column if not exists random_seed integer,
  add column if not exists created_at timestamptz not null default now();

-- Older installations linked simulation_runs to prediction_runs. Step 5 uses
-- the active daily-pipeline model_runs table, so the legacy link is optional.
do $$
begin
  if exists (
    select 1
    from information_schema.columns
    where table_schema = 'public'
      and table_name = 'simulation_runs'
      and column_name = 'prediction_run_id'
  ) then
    alter table public.simulation_runs
      alter column prediction_run_id drop not null;
  end if;

  if not exists (
    select 1
    from pg_constraint
    where conname = 'simulation_runs_model_run_id_fkey'
      and conrelid = 'public.simulation_runs'::regclass
  ) then
    alter table public.simulation_runs
      add constraint simulation_runs_model_run_id_fkey
      foreign key (model_run_id) references public.model_runs(id) on delete set null;
  end if;
end
$$;

create table if not exists public.team_simulation_results (
  simulation_run_id uuid not null
    references public.simulation_runs(id) on delete cascade,
  team_id text not null,
  group_stage_exit_probability double precision not null,
  round_of_32_probability double precision not null,
  round_of_16_probability double precision not null,
  quarterfinal_probability double precision not null,
  semifinal_probability double precision not null,
  final_probability double precision not null,
  champion_probability double precision not null,
  created_at timestamptz not null default now(),
  primary key (simulation_run_id, team_id)
);

create index if not exists simulation_runs_created_at_idx
  on public.simulation_runs (created_at desc);

create index if not exists simulation_runs_model_run_id_idx
  on public.simulation_runs (model_run_id);

create index if not exists team_simulation_results_team_idx
  on public.team_simulation_results (team_id, simulation_run_id);
