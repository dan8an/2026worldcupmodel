import express from "express";
import cors from "cors";
import dotenv from "dotenv";
import pg from "pg";
import {
  buildPlaceholderMatches,
  mergeTeams,
  normalizeDatabaseMatches,
  normalizeDatabaseSimulation,
  normalizeTeam,
  snapshotSimulation,
} from "./api-data.js";

dotenv.config();

const app = express();
app.use(cors());
app.use(express.json());

const db = new pg.Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: { rejectUnauthorized: false },
});

app.get("/api/health", (req, res) => {
  res.json({ ok: true });
});

app.get("/api/teams", async (req, res) => {
  try {
    const result = await db.query("select * from teams");
    res.json(mergeTeams(result.rows));
  } catch (err) {
    console.error("Supabase teams unavailable; using seed teams:", err.message);
    res.json(mergeTeams());
  }
});

app.get("/api/predictions", async (req, res) => {
  try {
    const result = await db.query("select * from predictions order by created_at desc");
    res.json(result.rows);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "Failed to fetch predictions" });
  }
});

const loadMatches = async () => {
  let databaseTeamRows = [];
  let matchRows = [];
  let predictionRows = [];

  try {
    const [teamsResult, matchesResult, predictionsResult] = await Promise.all([
      db.query("select * from teams"),
      db.query("select * from matches"),
      db.query("select * from predictions order by created_at desc"),
    ]);
    databaseTeamRows = teamsResult.rows;
    matchRows = matchesResult.rows;
    predictionRows = predictionsResult.rows;
  } catch (err) {
    console.error("Supabase matches unavailable; using fixture snapshot:", err.message);
  }

  const teams = databaseTeamRows.length
    ? mergeTeams(databaseTeamRows)
    : mergeTeams();

  if (!matchRows.length) {
    return buildPlaceholderMatches(teams);
  }

  return normalizeDatabaseMatches(
    matchRows,
    predictionRows,
    teams,
    databaseTeamRows,
  );
};

app.get("/api/matches", async (req, res) => {
  try {
    const matches = await loadMatches();
    const stage = typeof req.query.stage === "string" ? req.query.stage : null;
    res.json(stage ? matches.filter((match) => match.stage === stage) : matches);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "Failed to fetch matches" });
  }
});

app.get("/api/matches/:id", async (req, res) => {
  try {
    const matches = await loadMatches();
    const match = matches.find((item) => item.id === req.params.id);
    if (!match) {
      return res.status(404).json({ error: "Match not found" });
    }
    return res.json(match);
  } catch (err) {
    console.error(err);
    return res.status(500).json({ error: "Failed to fetch match" });
  }
});

const loadLatestSimulation = async () => {
  try {
    const runResult = await db.query(`
      select
        sr.*,
        pr.generated_at,
        pr.data_cutoff,
        mv.semantic_version as model_version
      from simulation_runs sr
      left join prediction_runs pr on pr.id = sr.prediction_run_id
      left join model_versions mv on mv.id = pr.model_version_id
      order by sr.created_at desc
      limit 1
    `);

    if (runResult.rows[0]) {
      const run = runResult.rows[0];
      const [probabilitiesResult, teamsResult] = await Promise.all([
        db.query(
          "select * from team_tournament_probabilities where simulation_run_id = $1",
          [run.id],
        ),
        db.query("select * from teams"),
      ]);

      if (probabilitiesResult.rows.length) {
        const teams = teamsResult.rows.map(normalizeTeam);
        return normalizeDatabaseSimulation(run, probabilitiesResult.rows, teams);
      }
    }
  } catch (err) {
    if (err.code !== "42P01" && err.code !== "42703") {
      console.error("Supabase simulation unavailable:", err.message);
    }
  }

  return snapshotSimulation();
};

app.get("/api/simulations/latest", async (req, res) => {
  const simulation = await loadLatestSimulation();
  if (!simulation) {
    return res.status(503).json({ error: "Simulation data is not available" });
  }
  return res.json(simulation);
});

const PORT = process.env.PORT || 3001;

if (process.env.NODE_ENV !== "test") {
  app.listen(PORT, () => {
    console.log(`Backend running on port ${PORT}`);
  });
}

export { app, db };
