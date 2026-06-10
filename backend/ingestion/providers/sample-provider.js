import { readFileSync } from "node:fs";
import { SportsProvider } from "./sports-provider.js";

const sampleData = JSON.parse(
  readFileSync(
    new URL("../sample-data/api-football.json", import.meta.url),
    "utf8",
  ),
);

export class SampleSportsProvider extends SportsProvider {
  constructor({ logger = console } = {}) {
    super("sample");
    this.logger = logger;
  }

  async get_completed_matches(date) {
    this.logger.info(`[sports:sample] Loading completed matches for ${date}`);
    return sampleData.matches.filter((match) => match.date.startsWith(date));
  }

  async get_fixture_statistics(fixtureId) {
    return sampleData.statistics[String(fixtureId)] ?? [];
  }

  async get_fixture_players(fixtureId) {
    return sampleData.players[String(fixtureId)] ?? [];
  }

  async get_lineups(fixtureId) {
    return sampleData.lineups[String(fixtureId)] ?? [];
  }
}
