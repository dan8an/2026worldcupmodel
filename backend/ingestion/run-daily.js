import dotenv from "dotenv";
import pg from "pg";
import { createSportsProvider } from "./providers/index.js";
import { SportsIngestionRepository } from "./repository.js";

dotenv.config();

const dateArgument = process.argv.find((argument) => argument.startsWith("--date="));
const date = dateArgument?.split("=")[1] ??
  new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString().slice(0, 10);

if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
  throw new Error("Use --date=YYYY-MM-DD");
}
if (!process.env.DATABASE_URL) {
  throw new Error("DATABASE_URL is required");
}

const logger = console;
const provider = createSportsProvider(process.env, { logger });
const pool = new pg.Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: { rejectUnauthorized: false },
});
const repository = new SportsIngestionRepository(pool, { logger });

try {
  await repository.assertSchema();
  logger.info(
    `[ingestion] Starting ${provider.name} completed-match ingestion for ${date}`,
  );
  const matches = await provider.get_completed_matches(date);
  logger.info(`[ingestion] Found ${matches.length} completed fixtures`);

  let succeeded = 0;
  let failed = 0;
  for (const fixture of matches) {
    try {
      logger.info(
        `[ingestion] Fixture ${fixture.providerFixtureId}: ` +
        `${fixture.homeTeam.name} vs ${fixture.awayTeam.name}`,
      );
      const [statistics, players, lineups] = await Promise.all([
        provider.get_fixture_statistics(fixture.providerFixtureId),
        provider.get_fixture_players(fixture.providerFixtureId),
        provider.get_lineups(fixture.providerFixtureId),
      ]);
      const result = await repository.ingestFixture({
        fixture,
        statistics,
        players,
        lineups,
      });
      succeeded += 1;
      logger.info(
        `[ingestion] Stored fixture ${fixture.providerFixtureId}: ` +
        `${result.teamStats} team rows, ${result.playerStats} player rows`,
      );
    } catch (error) {
      failed += 1;
      logger.error(
        `[ingestion] Fixture ${fixture.providerFixtureId} failed: ${error.message}`,
      );
    }
  }

  logger.info(
    `[ingestion] Finished ${date}: ${succeeded} succeeded, ${failed} failed`,
  );
  if (failed > 0) process.exitCode = 1;
} finally {
  await pool.end();
}
