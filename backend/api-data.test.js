import test from "node:test";
import assert from "node:assert/strict";
import {
  buildPlaceholderMatches,
  mergeTeams,
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
