import test from "node:test";
import assert from "node:assert/strict";
import {
  buildPlaceholderMatches,
  mergeDatabaseMatches,
  mergeCanonicalPredictions,
  mergeTeams,
  normalizeDatabaseMatches,
  normalizeDatabaseSimulation,
  snapshotSimulation,
} from "./api-data.js";

test("placeholder group fixtures match the frontend contract", () => {
  const matches = buildPlaceholderMatches();

  assert.equal(matches.length, 72);
  assert.ok(matches.every((match) => match.stage === "group"));
  assert.ok(matches.every((match) => match.home_team && match.away_team));
  assert.deepEqual(
    matches.map((match) => match.kickoff),
    [...matches].map((match) => match.kickoff).sort(),
  );
  assert.equal(matches[0].prediction?.match_id, matches[0].id);
});

test("database team ratings are merged into canonical tournament teams", () => {
  const teams = mergeTeams([
    {
      id: "database-uuid",
      name: "Argentina",
      fifa_rank: 1,
      elo_rating: 2140,
    },
  ]);
  const argentina = teams.find((team) => team.id === "ARG");

  assert.equal(teams.length, 48);
  assert.equal(argentina.elo, 2140);
  assert.equal(argentina.rank, 1);
});

test("simulation snapshot satisfies the expected response shape", () => {
  const simulation = snapshotSimulation();

  assert.equal(simulation.iterations, 50000);
  assert.equal(simulation.teams.length, 48);
  assert.equal(typeof simulation.model_version, "string");
  assert.equal(typeof simulation.monte_carlo_precision.worst_case_95_margin, "number");
});

test("provider matches do not replace the World Cup forecast catalog", () => {
  const teams = mergeTeams();
  const canonicalMatches = buildPlaceholderMatches(teams);
  const databaseTeams = [
    { id: "mexico-uuid", name: "Mexico" },
    { id: "south-africa-uuid", name: "South Africa" },
  ];
  const databaseMatches = normalizeDatabaseMatches(
    [
      {
        id: "provider-match-uuid",
        home_team_id: "mexico-uuid",
        away_team_id: "south-africa-uuid",
        match_date: "2025-06-11T17:00:00Z",
        tournament_stage: "Friendlies",
      },
    ],
    [],
    teams,
    databaseTeams,
  );

  const matches = mergeDatabaseMatches(canonicalMatches, databaseMatches);

  assert.equal(matches.length, 72);
  assert.ok(matches.every((match) => match.stage === "group"));
  assert.equal(matches[0].id, "WC26-001");
  assert.equal(typeof matches[0].prediction.home_xg, "number");
  assert.equal(typeof matches[0].prediction.probabilities.home_win, "number");
});

test("matching database predictions enrich canonical fixtures without losing details", () => {
  const teams = mergeTeams();
  const canonicalMatches = buildPlaceholderMatches(teams);
  const databaseTeams = [
    { id: "mexico-uuid", name: "Mexico" },
    { id: "south-africa-uuid", name: "South Africa" },
  ];
  const databaseMatches = normalizeDatabaseMatches(
    [
      {
        id: "provider-match-uuid",
        home_team_id: "mexico-uuid",
        away_team_id: "south-africa-uuid",
        match_date: canonicalMatches[0].kickoff,
        tournament_stage: "Group Stage - 1",
      },
    ],
    [
      {
        match_id: "provider-match-uuid",
        home_xg: 2.1,
        away_xg: 0.7,
        home_win_probability: 0.65,
        draw_probability: 0.22,
        away_win_probability: 0.13,
        confidence_tier: "Medium",
        prediction_timestamp: "2026-06-10T18:00:00Z",
      },
      {
        match_id: "provider-match-uuid",
        home_xg: 0.1,
        away_xg: 3.4,
        home_win_prob: 0.05,
        draw_prob: 0.1,
        away_win_prob: 0.85,
        confidence_tier: "Older prediction",
      },
    ],
    teams,
    databaseTeams,
  );

  const [match] = mergeDatabaseMatches(canonicalMatches, databaseMatches);

  assert.equal(match.id, "WC26-001");
  assert.equal(match.stage, "group");
  assert.equal(match.prediction.match_id, "WC26-001");
  assert.equal(match.prediction.home_xg, 2.1);
  assert.equal(match.prediction.probabilities.home_win, 0.65);
  assert.equal(match.prediction.generated_at, "2026-06-10T18:00:00Z");
  assert.ok(match.prediction.top_scores.length > 0);
  assert.ok(match.prediction.key_factors.length > 0);
});

test("canonical predictions attach without database match rows", () => {
  const [match] = mergeCanonicalPredictions(buildPlaceholderMatches(), [
      {
        canonical_match_id: "WC26-001",
      home_xg: 1.8,
      away_xg: 0.9,
      home_win_probability: 0.58,
      draw_probability: 0.25,
        away_win_probability: 0.17,
        elo_base_home_probability: 0.55,
        elo_base_draw_probability: 0.27,
        elo_base_away_probability: 0.18,
        attack_defense_adjustment: 0.02,
        draw_calibration_adjustment: 0.01,
        context_adjustment_total: 0.03,
        final_home_probability: 0.58,
        final_draw_probability: 0.25,
        final_away_probability: 0.17,
        confidence_score: 0.63,
        top_factors: [
          { factor: "Elo advantage", team: "Mexico", impact: "+4.0%" },
        ],
        model_version: "elo-context-v3",
      prediction_timestamp: "2026-06-10T19:00:00Z",
    },
  ]);

  assert.equal(match.id, "WC26-001");
  assert.equal(match.prediction.home_xg, 1.8);
  assert.equal(match.prediction.probabilities.home_win, 0.58);
  assert.equal(match.prediction.model_version, "elo-context-v3");
  assert.equal(match.prediction.final_home_probability, 0.58);
  assert.equal(match.prediction.top_factors[0].factor, "Elo advantage");
});

test("persisted Step 5 simulation rows retain the frontend shape", () => {
  const [row] = normalizeDatabaseSimulation(
    {
      num_simulations: 50000,
      random_seed: 2026,
      model_version: "elo-context-v3",
      created_at: "2026-06-10T20:00:00Z",
    },
    [
      {
        team_id: "ARG",
        group_stage_exit_probability: 0.05,
        round_of_32_probability: 0.95,
        round_of_16_probability: 0.7,
        quarterfinal_probability: 0.5,
        semifinal_probability: 0.3,
        final_probability: 0.18,
        champion_probability: 0.1,
      },
    ],
    mergeTeams(),
  ).teams;

  assert.equal(row.team_id, "ARG");
  assert.equal(row.team_name, "Argentina");
  assert.equal(row.group, "J");
  assert.equal(row.confederation, "CONMEBOL");
  assert.equal(row.round_of_32, 0.95);
  assert.equal(row.champion, 0.1);
});

test("all canonical simulation rows resolve non-empty team labels", () => {
  const teams = mergeTeams([
    {
      id: "argentina-database-uuid",
      name: "Argentina",
      confederation: "CONMEBOL",
    },
  ]);
  const simulation = normalizeDatabaseSimulation(
    { num_simulations: 50000 },
    teams.map((team) => ({
      team_id: team.id,
      round_of_32_probability: 0.5,
      round_of_16_probability: 0.25,
      quarterfinal_probability: 0.125,
      semifinal_probability: 0.06,
      final_probability: 0.03,
      champion_probability: 0.015,
    })),
    teams,
  );

  assert.equal(simulation.teams.length, 48);
  assert.ok(simulation.teams.every((team) => team.team_name.trim().length > 0));
});
