import test from "node:test";
import assert from "node:assert/strict";
import { once } from "node:events";
import { app, db } from "./index.js";
import { mergeTeams } from "./api-data.js";

const databaseTeams = [
  {
    id: "argentina-uuid",
    name: "Argentina",
    fifa_rank: 1,
    elo_rating: 2140,
    confederation: "CONMEBOL",
  },
];
const simulationRows = mergeTeams().map((team, index) => ({
  simulation_run_id: "simulation-run",
  team_id: team.id,
  group_stage_exit_probability: 0.25,
  round_of_32_probability: 0.75,
  round_of_16_probability: 0.4,
  quarterfinal_probability: 0.2,
  semifinal_probability: 0.1,
  final_probability: 0.05,
  champion_probability: index === 0 ? 0.1 : 0.9 / 47,
}));
const predictionRows = [{
  canonical_match_id: "WC26-001",
  home_win_probability: 0.55,
  draw_probability: 0.27,
  away_win_probability: 0.18,
  elo_base_home_probability: 0.52,
  elo_base_draw_probability: 0.28,
  elo_base_away_probability: 0.2,
  attack_defense_adjustment: 0.02,
  draw_calibration_adjustment: 0.01,
  context_adjustment_total: 0.03,
  final_home_probability: 0.55,
  final_draw_probability: 0.27,
  final_away_probability: 0.18,
  confidence_score: 0.61,
  top_factors: [
    { factor: "Elo advantage", team: "Mexico", impact: "+3.0%" },
  ],
  model_version: "elo-context-v3",
  prediction_timestamp: "2026-06-10T20:00:00Z",
}];

db.query = async (sql) => {
  if (sql.includes("from simulation_runs")) {
    return {
      rows: [{
        id: "simulation-run",
        num_simulations: 50000,
        random_seed: 2026,
        model_version: "elo-context-v3",
        created_at: "2026-06-10T20:00:00Z",
      }],
    };
  }
  if (sql.includes("from team_simulation_results")) return { rows: simulationRows };
  if (sql.includes("from teams")) return { rows: databaseTeams };
  if (sql.includes("from matches")) return { rows: [] };
  if (sql.includes("from predictions")) return { rows: predictionRows };
  throw new Error(`Unexpected test query: ${sql}`);
};

const server = app.listen(0, "127.0.0.1");
await once(server, "listening");
const address = server.address();
const baseUrl = `http://127.0.0.1:${address.port}`;

test.after(async () => {
  await new Promise((resolve) => server.close(resolve));
  await db.end();
});

test("GET /api/matches?stage=group returns chronological match objects", async () => {
  const response = await fetch(`${baseUrl}/api/matches?stage=group`);
  const matches = await response.json();

  assert.equal(response.status, 200);
  assert.equal(matches.length, 72);
  assert.equal(matches[0].stage, "group");
  assert.equal(typeof matches[0].home_team.name, "string");
  assert.equal(typeof matches[0].prediction.probabilities.home_win, "number");
  const probabilities = Object.values(matches[0].prediction.probabilities);
  assert.ok(probabilities.every((value) => value >= 0 && value <= 1));
  assert.ok(Math.abs(probabilities.reduce((sum, value) => sum + value, 0) - 1) < 1e-6);
  assert.equal(matches[0].prediction.final_home_probability, 0.55);
  assert.equal(matches[0].prediction.top_factors[0].factor, "Elo advantage");
});

test("GET /api/matches/:id returns a single match", async () => {
  const response = await fetch(`${baseUrl}/api/matches/WC26-001`);
  const match = await response.json();

  assert.equal(response.status, 200);
  assert.equal(match.id, "WC26-001");
  assert.equal(match.home_team.id, "MEX");
});

test("GET /api/simulations/latest returns the frontend simulation shape", async () => {
  const response = await fetch(`${baseUrl}/api/simulations/latest`);
  const simulation = await response.json();

  assert.equal(response.status, 200);
  assert.equal(simulation.iterations, 50000);
  assert.equal(simulation.model_version, "elo-context-v3");
  assert.equal(simulation.teams.length, 48);
  assert.equal(typeof simulation.teams[0].champion, "number");
  assert.ok(simulation.teams.every((team) => team.champion >= 0 && team.champion <= 1));
  assert.ok(simulation.teams.every((team) => team.team_name.trim()));
  assert.ok(simulation.teams.every((team) => team.team_id.length === 3));
});

test("GET /api/teams returns canonical teams with names", async () => {
  const response = await fetch(`${baseUrl}/api/teams`);
  const teams = await response.json();

  assert.equal(response.status, 200);
  assert.equal(teams.length, 48);
  assert.ok(teams.every((team) => team.id && team.name));
});
