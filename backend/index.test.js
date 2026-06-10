import test from "node:test";
import assert from "node:assert/strict";
import { once } from "node:events";
import { app, db } from "./index.js";

const databaseTeams = [
  {
    id: "argentina-uuid",
    name: "Argentina",
    fifa_rank: 1,
    elo_rating: 2140,
    confederation: "CONMEBOL",
  },
];

db.query = async (sql) => {
  if (sql.includes("from simulation_runs")) {
    const error = new Error("relation does not exist");
    error.code = "42P01";
    throw error;
  }
  if (sql.includes("from teams")) return { rows: databaseTeams };
  if (sql.includes("from matches")) return { rows: [] };
  if (sql.includes("from predictions")) return { rows: [] };
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
  assert.equal(simulation.teams.length, 48);
  assert.equal(typeof simulation.teams[0].champion, "number");
});
