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

test("placeholder tournament fixtures include group stage only", () => {
  const matches = buildPlaceholderMatches();

  assert.equal(matches.length, 72);
  assert.ok(matches.every((match) => match.stage === "group"));
  assert.ok(matches.every((match) => match.home_team && match.away_team));
  assert.deepEqual(
    matches.map((match) => match.kickoff),
    [...matches].map((match) => match.kickoff).sort(),
  );
  assert.equal(matches[0].prediction?.match_id, matches[0].id);
  assert.equal(matches.find((match) => match.stage !== "group"), undefined);
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
  assert.equal(simulation.source, "fallback_static");
  assert.equal(simulation.created_at, simulation.generated_at);
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

test("provider result near a UTC boundary merges into the canonical USA-Paraguay fixture", () => {
  const teams = mergeTeams();
  const canonicalMatches = buildPlaceholderMatches(teams);
  const databaseTeams = [
    { id: "usa-uuid", name: "United States" },
    { id: "paraguay-uuid", name: "Paraguay" },
  ];
  const databaseMatches = normalizeDatabaseMatches(
    [
      {
        id: "provider-match-uuid",
        home_team_id: "usa-uuid",
        away_team_id: "paraguay-uuid",
        match_date: "2026-06-13T01:00:00Z",
        tournament_stage: "Group Stage - 1",
        home_score: 4,
        away_score: 1,
        completed: true,
      },
    ],
    [],
    teams,
    databaseTeams,
  );

  const match = mergeDatabaseMatches(canonicalMatches, databaseMatches)
    .find((candidate) => candidate.id === "WC26-019");

  assert.equal(match.status, "completed");
  assert.equal(match.home_score, 4);
  assert.equal(match.away_score, 1);
});

test("provider timestamp still matches Korea-Czechia canonical fixture by kickoff proximity", () => {
  const teams = mergeTeams();
  const canonicalMatches = buildPlaceholderMatches(teams);
  const databaseTeams = [
    { id: "korea-uuid", name: "South Korea" },
    { id: "czechia-uuid", name: "Czechia" },
  ];
  const databaseMatches = normalizeDatabaseMatches(
    [
      {
        id: "provider-match-uuid",
        home_team_id: "korea-uuid",
        away_team_id: "czechia-uuid",
        match_date: "2026-06-12T01:00:00Z",
        tournament_stage: "Group Stage - 1",
        home_score: 2,
        away_score: 0,
        completed: true,
      },
    ],
    [],
    teams,
    databaseTeams,
  );

  const match = mergeDatabaseMatches(canonicalMatches, databaseMatches)
    .find((candidate) => candidate.id === "WC26-002");

  assert.equal(match.status, "completed");
  assert.equal(match.home_score, 2);
  assert.equal(match.away_score, 0);
});

test("no generated knockout placeholders are shown without provider fixtures", () => {
  const matches = mergeDatabaseMatches(buildPlaceholderMatches(), []);

  assert.equal(matches.length, 72);
  assert.equal(matches.find((match) => match.stage !== "group"), undefined);
  assert.equal(matches.find((match) => match.id === "WC26-073"), undefined);
});

test("scheduled and completed knockout database rows are appended as real fixtures", () => {
  const teams = mergeTeams();
  const canonicalMatches = buildPlaceholderMatches(teams);
  const databaseTeams = [
    { id: "mexico-uuid", name: "Mexico" },
    { id: "south-africa-uuid", name: "South Africa" },
  ];
  const databaseMatches = normalizeDatabaseMatches(
    [
      {
        id: "provider-knockout",
        match_number: 73,
        home_team_id: "mexico-uuid",
        away_team_id: "south-africa-uuid",
        match_date: "2026-06-28T16:00:00Z",
        tournament_stage: "Round of 32",
        status: "scheduled",
      },
      {
        id: "provider-knockout-final",
        match_number: 89,
        home_team_id: "mexico-uuid",
        away_team_id: "south-africa-uuid",
        match_date: "2026-07-04T16:00:00Z",
        tournament_stage: "Round of 16",
        completed: true,
        home_score: 1,
        away_score: 0,
      },
    ],
    [],
    teams,
    databaseTeams,
  );

  const merged = mergeDatabaseMatches(canonicalMatches, databaseMatches);
  const scheduled = merged.find((match) => match.id === "provider-knockout");
  const completed = merged.find((match) => match.id === "provider-knockout-final");

  assert.equal(merged.length, 74);
  assert.equal(scheduled.status, "scheduled");
  assert.equal(scheduled.stage, "round_of_32");
  assert.equal(scheduled.home_team.name, "Mexico");
  assert.equal(completed.status, "completed");
  assert.equal(completed.stage, "round_of_16");
  assert.equal(completed.home_score, 1);
  assert.equal(completed.home_team.name, "Mexico");
});

test("completed knockout prediction survives UUID dedupe via provider fixture identity", () => {
  const teams = mergeTeams();
  const databaseTeams = [
    { id: "mexico-uuid", name: "Mexico" },
    { id: "south-africa-uuid", name: "South Africa" },
  ];
  const predictions = [
    {
      match_id: "current-qf-uuid",
      provider_fixture_id: 99097,
      prediction_timestamp: "2026-07-09T22:00:00Z",
      home_win_probability: 0.99,
      draw_probability: 0.005,
      away_win_probability: 0.005,
    },
    {
      match_id: "old-qf-uuid",
      provider_fixture_id: 99097,
      prediction_timestamp: "2026-07-09T18:00:00Z",
      home_win_probability: 0.63,
      draw_probability: 0.22,
      away_win_probability: 0.15,
    },
  ];
  const matches = normalizeDatabaseMatches(
    [{
      id: "current-qf-uuid",
      api_football_fixture_id: 99097,
      match_number: 97,
      home_team_id: "mexico-uuid",
      away_team_id: "south-africa-uuid",
      match_date: "2026-07-09T20:00:00Z",
      tournament_stage: "Quarter-finals",
      status: "FT",
      home_score: 2,
      away_score: 1,
    }],
    predictions,
    teams,
    databaseTeams,
  );

  assert.equal(matches[0].prediction.probabilities.home_win, 0.63);
  assert.equal(matches[0].prediction.generated_at, "2026-07-09T18:00:00Z");
});

test("authentic prediction precedes a selectable historical backfill", () => {
  const teams = mergeTeams();
  const databaseTeams = [
    { id: "mexico-uuid", name: "Mexico" },
    { id: "south-africa-uuid", name: "South Africa" },
  ];
  const row = {
    id: "r16-uuid", match_number: 89,
    home_team_id: "mexico-uuid", away_team_id: "south-africa-uuid",
    match_date: "2026-07-04T20:00:00Z", tournament_stage: "Round of 16", status: "FT",
  };
  const backfill = {
    match_id: "r16-uuid", prediction_timestamp: "2026-07-10T00:00:00Z",
    generation_mode: "historical_backfill", historical_cutoff: "2026-07-04T19:59:59Z",
    home_win_probability: 0.7, draw_probability: 0.2, away_win_probability: 0.1,
  };
  const onlyBackfill = normalizeDatabaseMatches([row], [backfill], teams, databaseTeams);
  assert.equal(onlyBackfill[0].prediction.probabilities.home_win, 0.7);
  assert.equal(onlyBackfill[0].prediction.generation_mode, "historical_backfill");

  const authentic = {
    match_id: "r16-uuid", prediction_timestamp: "2026-07-04T18:00:00Z",
    generation_mode: "standard", home_win_probability: 0.5,
    draw_probability: 0.3, away_win_probability: 0.2,
  };
  const both = normalizeDatabaseMatches([row], [backfill, authentic], teams, databaseTeams);
  assert.equal(both[0].prediction.probabilities.home_win, 0.5);
  assert.equal(both[0].prediction.generation_mode, "standard");
});

test("knockout prediction resolves by official canonical number", () => {
  const teams = mergeTeams();
  const databaseTeams = [
    { id: "argentina-uuid", name: "Argentina" },
    { id: "france-uuid", name: "France" },
  ];
  const matches = normalizeDatabaseMatches(
    [{
      id: "current-qf-98",
      match_number: 98,
      home_team_id: "argentina-uuid",
      away_team_id: "france-uuid",
      match_date: "2026-07-10T20:00:00Z",
      tournament_stage: "Quarter-finals",
      status: "AET",
    }],
    [{
      canonical_match_id: "WC26-098",
      prediction_timestamp: "2026-07-10T17:00:00Z",
      home_win_probability: 0.44,
      draw_probability: 0.30,
      away_win_probability: 0.26,
    }],
    teams,
    databaseTeams,
  );

  assert.equal(matches[0].prediction.probabilities.home_win, 0.44);
});

test("post-kickoff and similar-fixture predictions do not attach to knockout", () => {
  const teams = mergeTeams();
  const databaseTeams = [
    { id: "mexico-uuid", name: "Mexico" },
    { id: "south-africa-uuid", name: "South Africa" },
  ];
  const matches = normalizeDatabaseMatches(
    [{
      id: "current-qf-uuid",
      match_number: 97,
      home_team_id: "mexico-uuid",
      away_team_id: "south-africa-uuid",
      match_date: "2026-07-09T20:00:00Z",
      tournament_stage: "Quarter-finals",
      status: "FT",
    }],
    [
      {
        match_id: "current-qf-uuid",
        prediction_timestamp: "2026-07-09T21:00:00Z",
        home_win_probability: 0.9,
      },
      {
        home_team_id: "MEX",
        away_team_id: "RSA",
        kickoff: "2026-07-12T20:00:00Z",
        prediction_timestamp: "2026-07-09T18:00:00Z",
        home_win_probability: 0.8,
      },
    ],
    teams,
    databaseTeams,
  );

  assert.equal(matches[0].prediction, null);
});

test("provider knockout fixture does not duplicate a generated placeholder", () => {
  const teams = mergeTeams();
  const databaseTeams = [
    { id: "mexico-uuid", name: "Mexico" },
    { id: "south-africa-uuid", name: "South Africa" },
  ];
  const databaseMatches = normalizeDatabaseMatches(
    [
      {
        id: "provider-knockout",
        match_number: 73,
        home_team_id: "mexico-uuid",
        away_team_id: "south-africa-uuid",
        match_date: "2026-06-28T16:00:00Z",
        tournament_stage: "Round of 32",
        status: "scheduled",
      },
    ],
    [],
    teams,
    databaseTeams,
  );

  const merged = mergeDatabaseMatches(buildPlaceholderMatches(teams), databaseMatches);
  const knockouts = merged.filter((match) => match.stage === "round_of_32");

  assert.equal(knockouts.length, 1);
  assert.equal(knockouts[0].id, "provider-knockout");
});

test("official knockout matches are filtered, deduped, and capped by stage", () => {
  const teams = mergeTeams();
  const databaseTeams = [
    { id: "mexico-uuid", name: "Mexico" },
    { id: "south-africa-uuid", name: "South Africa" },
  ];
  const rows = [];
  const stageNumbers = {
    "Round of 32": [73, 88],
    "Round of 16": [89, 96],
    "Quarter-finals": [97, 100],
    "Semi-finals": [101, 102],
    "Third-place": [103, 103],
    Final: [104, 104],
  };
  for (const [stage, [start, end]] of Object.entries(stageNumbers)) {
    for (let number = start; number <= end; number += 1) {
      rows.push({
        id: `provider-${number}`,
        match_number: number,
        home_team_id: "mexico-uuid",
        away_team_id: "south-africa-uuid",
        match_date: `2026-07-${String(Math.min(19, Math.max(1, number - 72))).padStart(2, "0")}T16:00:00Z`,
        tournament_stage: stage,
        status: "scheduled",
      });
    }
  }
  rows.push(
    {
      id: "scheduled-duplicate-73",
      match_number: 73,
      home_team_id: "mexico-uuid",
      away_team_id: "south-africa-uuid",
      match_date: "2026-06-28T16:00:00Z",
      tournament_stage: "Round of 32",
      status: "scheduled",
    },
    {
      id: "completed-duplicate-73",
      match_number: 73,
      home_team_id: "mexico-uuid",
      away_team_id: "south-africa-uuid",
      match_date: "2026-06-28T16:00:00Z",
      tournament_stage: "Round of 32",
      status: "finished",
      home_score: 2,
      away_score: 1,
      updated_at: "2026-06-28T20:00:00Z",
    },
    {
      id: "2148",
      home_team_id: "mexico-uuid",
      away_team_id: "south-africa-uuid",
      match_date: "2026-07-09T20:00:00Z",
      tournament_stage: "Quarter-finals",
      status: "scheduled",
    },
    {
      id: "historical-89",
      match_number: 89,
      home_team_id: "mexico-uuid",
      away_team_id: "south-africa-uuid",
      match_date: "2022-12-03T16:00:00Z",
      tournament_stage: "Round of 16",
      status: "finished",
      home_score: 1,
      away_score: 0,
    },
  );

  const merged = mergeDatabaseMatches(
    buildPlaceholderMatches(teams),
    normalizeDatabaseMatches(rows, [], teams, databaseTeams),
  );
  const knockouts = merged.filter((match) => match.stage !== "group");
  const counts = knockouts.reduce((accumulator, match) => {
    accumulator[match.stage] = (accumulator[match.stage] ?? 0) + 1;
    return accumulator;
  }, {});

  assert.equal(counts.round_of_32, 16);
  assert.equal(counts.round_of_16, 8);
  assert.equal(counts.quarterfinal, 4);
  assert.equal(counts.semifinal, 2);
  assert.equal(counts.third_place, 1);
  assert.equal(counts.final, 1);
  assert.equal(knockouts.find((match) => match.id === "2148"), undefined);
  assert.equal(knockouts.find((match) => match.id === "historical-89"), undefined);
  const match73 = knockouts.find((match) => match.number === 73);
  assert.equal(match73.id, "completed-duplicate-73");
  assert.equal(match73.home_score, 2);
  assert.equal(match73.away_score, 1);
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
        confidence_tier: "Medium",
        confidence_explanation: "Medium confidence because model inputs are incomplete.",
        top_factors: [
          { factor: "Elo advantage", team: "Mexico", impact: "+4.0%" },
          { factor: "Shot volume", team: "Mexico", impact: "+1.1%" },
        ],
        model_version: "elo-context-v4.1",
      prediction_timestamp: "2026-06-10T19:00:00Z",
    },
  ]);

  assert.equal(match.id, "WC26-001");
  assert.equal(match.prediction.home_xg, 1.8);
  assert.equal(match.prediction.probabilities.home_win, 0.58);
  assert.equal(match.prediction.model_version, "elo-context-v4.1");
  assert.equal(match.prediction.final_home_probability, 0.58);
  assert.equal(match.prediction.top_factors[0].factor, "Elo advantage");
  assert.equal(match.prediction.top_factors[1].factor, "Shot volume");
  assert.equal(match.prediction.confidence_tier, "Medium");
  assert.match(match.prediction.confidence_explanation, /inputs are incomplete/);
});

test("persisted Step 5 simulation rows retain the frontend shape", () => {
  const [row] = normalizeDatabaseSimulation(
    {
      num_simulations: 50000,
      random_seed: 2026,
      model_version: "elo-context-v4.1",
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
