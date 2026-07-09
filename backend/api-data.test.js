import test from "node:test";
import assert from "node:assert/strict";
import {
  buildPlaceholderMatches,
  mergeDatabaseMatches,
  mergeCanonicalPredictions,
  mergeTeams,
  normalizeDatabaseMatches,
  normalizeDatabaseSimulation,
  resolveKnockoutParticipants,
  snapshotSimulation,
} from "./api-data.js";

test("placeholder tournament fixtures match the frontend contract", () => {
  const matches = buildPlaceholderMatches();

  assert.equal(matches.length, 104);
  assert.equal(matches.filter((match) => match.stage === "group").length, 72);
  assert.equal(matches.filter((match) => match.stage !== "group").length, 32);
  assert.ok(matches.filter((match) => match.stage === "group").every((match) => match.home_team && match.away_team));
  assert.ok(matches.filter((match) => match.stage !== "group").every((match) => !match.home_team && !match.away_team));
  assert.deepEqual(
    matches.map((match) => match.kickoff),
    [...matches].map((match) => match.kickoff).sort(),
  );
  assert.equal(matches[0].prediction?.match_id, matches[0].id);
  assert.equal(matches.find((match) => match.id === "WC26-089").home_slot, "Winner Round of 32 Match 1");
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

  assert.equal(matches.length, 104);
  assert.equal(matches.filter((match) => match.stage === "group").length, 72);
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

test("unresolved knockout fixtures remain visible with friendly placeholders", () => {
  const match = buildPlaceholderMatches().find((candidate) => candidate.id === "WC26-073");

  assert.equal(match.stage, "round_of_32");
  assert.equal(match.home_team, null);
  assert.equal(match.away_team, null);
  assert.equal(match.home_slot, "Round of 32 home qualifier 1");
  assert.equal(match.away_slot, "Round of 32 away qualifier 1");
});

test("completed group stage resolves round of 32 teams instead of placeholder numbers", () => {
  const matches = buildPlaceholderMatches().map((match) =>
    match.stage === "group"
      ? { ...match, status: "finished", home_score: 2, away_score: 0 }
      : match
  );

  const resolved = resolveKnockoutParticipants(matches);
  const firstKnockout = resolved.find((match) => match.id === "WC26-073");

  assert.equal(typeof firstKnockout.home_team.name, "string");
  assert.equal(firstKnockout.home_team.flag.length > 0, true);
  assert.equal(firstKnockout.home_slot, null);
  assert.equal(/^\d+$/.test(firstKnockout.home_team.name), false);
});

test("completed knockout winners feed later rounds", () => {
  const teams = mergeTeams();
  const matches = buildPlaceholderMatches(teams).map((match) => {
    if (match.id === "WC26-073") {
      return {
        ...match,
        home_team: teams.find((team) => team.id === "MEX"),
        away_team: teams.find((team) => team.id === "RSA"),
        home_slot: null,
        away_slot: null,
        status: "finished",
        home_score: 3,
        away_score: 1,
      };
    }
    return match;
  });

  const roundOf16 = resolveKnockoutParticipants(matches)
    .find((match) => match.id === "WC26-089");

  assert.equal(roundOf16.home_team.name, "Mexico");
  assert.equal(roundOf16.away_team, null);
  assert.equal(roundOf16.away_slot, "Winner Round of 32 Match 2");
});

test("scheduled and completed knockout database rows merge into canonical fixtures", () => {
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
  const scheduled = merged.find((match) => match.id === "WC26-073");
  const completed = merged.find((match) => match.id === "WC26-089");

  assert.equal(scheduled.status, "scheduled");
  assert.equal(scheduled.home_team.name, "Mexico");
  assert.equal(completed.status, "completed");
  assert.equal(completed.home_score, 1);
  assert.equal(completed.home_team.name, "Mexico");
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
