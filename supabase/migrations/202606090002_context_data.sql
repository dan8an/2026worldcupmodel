create table data_sources (
  id text primary key,
  kind text not null,
  name text not null,
  base_url text,
  license text,
  enabled boolean not null default true,
  created_at timestamptz not null default now()
);

create table ingestion_runs (
  id uuid primary key default gen_random_uuid(),
  source_id text not null references data_sources(id),
  started_at timestamptz not null default now(),
  completed_at timestamptz,
  status text not null,
  records_read integer not null default 0,
  records_written integer not null default 0,
  payload_sha256 text,
  error_message text
);

create table historical_matches (
  id uuid primary key default gen_random_uuid(),
  source_id text not null references data_sources(id),
  source_match_id text,
  played_on date not null,
  home_team_id text not null references teams(id),
  away_team_id text not null references teams(id),
  home_score smallint not null check (home_score >= 0),
  away_score smallint not null check (away_score >= 0),
  tournament text not null,
  neutral boolean not null,
  source_updated_at timestamptz,
  ingested_at timestamptz not null default now(),
  unique (source_id, source_match_id),
  check (home_team_id <> away_team_id)
);

create index historical_matches_team_date_idx
  on historical_matches (played_on desc, home_team_id, away_team_id);

create table players (
  id uuid primary key default gen_random_uuid(),
  provider_key text unique,
  display_name text not null,
  date_of_birth date,
  primary_position text,
  created_at timestamptz not null default now()
);

create table squads (
  id uuid primary key default gen_random_uuid(),
  team_id text not null references teams(id),
  player_id uuid not null references players(id),
  tournament_id text not null default 'FIFA-WC-2026',
  squad_status text not null check (squad_status in ('preliminary', 'final', 'reserve', 'removed')),
  shirt_number smallint,
  source_url text not null,
  published_at timestamptz not null,
  ingested_at timestamptz not null default now(),
  unique (team_id, player_id, tournament_id, published_at)
);

create table availability_reports (
  id uuid primary key default gen_random_uuid(),
  team_id text not null references teams(id),
  player_id uuid references players(id),
  player_name text not null,
  status text not null check (
    status in ('out', 'omitted', 'doubtful', 'questionable', 'limited', 'available')
  ),
  importance double precision not null check (importance between 0 and 1),
  confidence double precision not null check (confidence between 0 and 1),
  source_name text not null,
  source_url text not null,
  published_at timestamptz not null,
  effective_from timestamptz not null,
  effective_until timestamptz,
  note text not null default '',
  ingested_at timestamptz not null default now(),
  check (effective_until is null or effective_until >= effective_from)
);

create index availability_reports_active_idx
  on availability_reports (team_id, effective_from, effective_until, published_at desc);

create table team_feature_snapshots (
  id uuid primary key default gen_random_uuid(),
  team_id text not null references teams(id),
  opponent_team_id text references teams(id),
  cutoff timestamptz not null,
  feature_schema_version text not null,
  features jsonb not null,
  created_at timestamptz not null default now(),
  unique (team_id, opponent_team_id, cutoff, feature_schema_version)
);
