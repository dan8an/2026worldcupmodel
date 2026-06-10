# Daily Pipeline Schema Assumptions

The migration `202606100001_daily_prediction_pipeline.sql` was written against
the live Supabase `public` schema inspected on June 10, 2026.

## Existing tables

The migration does not recreate these tables:

| Table | Existing primary key | Relevant existing columns |
| --- | --- | --- |
| `teams` | `id uuid` | `name`, `fifa_rank`, `elo_rating`, `confederation` |
| `matches` | `id uuid` | `home_team`, `away_team`, `match_date`, `tournament_stage` |
| `model_runs` | `id uuid` | `run_date`, `model_version`, `notes` |
| `predictions` | `id uuid` | `match_id`, result probabilities, `created_at` |

`predictions.match_id` already references `matches.id`.

## Additive compatibility columns

- `matches.home_team_id` and `matches.away_team_id` provide canonical UUID
  foreign keys while preserving the existing text columns used by current
  routes.
- `teams.api_football_team_id` and `matches.api_football_fixture_id` are
  nullable, unique provider identifiers used for repeat-safe ingestion.
- Matches retain the normalized provider name, raw payload, and update time
  without exposing provider credentials.
- `predictions.model_run_id` links predictions to the existing `model_runs`
  table. A partial unique index allows only one prediction per match and model
  run while leaving legacy rows with no model run unaffected.
- `model_runs` receives cutoff, status, seed, generation-time, and JSON
  metadata fields needed by a repeatable daily pipeline.
- Prediction xG, confidence, explanation, and cutoff fields are nullable so
  existing rows remain valid.

## New tables

Only missing pipeline tables are created: `players`, `team_match_stats`,
`player_match_stats`, `team_ratings`, `player_ratings`, and
`odds_snapshots`.

All additions use `IF NOT EXISTS` guards. Foreign-key creation is also guarded
by constraint-name checks, so rerunning the migration does not duplicate
objects. The migration does not delete or rewrite existing records.
