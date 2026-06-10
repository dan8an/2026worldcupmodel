import test from "node:test";
import assert from "node:assert/strict";
import { ApiFootballProvider } from "./api-football.js";
import { SampleSportsProvider } from "./sample-provider.js";
import { createSportsProvider } from "./index.js";

const silentLogger = {
  info() {},
  warn() {},
  error() {},
};

test("provider factory falls back to sample data without a key", () => {
  const provider = createSportsProvider(
    { SPORTS_PROVIDER: "api_football" },
    { logger: silentLogger },
  );
  assert.ok(provider instanceof SampleSportsProvider);
});

test("API-Football completed fixtures are normalized and filtered", async () => {
  let requestedUrl;
  let requestedHeaders;
  const provider = new ApiFootballProvider({
    apiKey: "test-key",
    logger: silentLogger,
    fetchImpl: async (url, options) => {
      requestedUrl = url;
      requestedHeaders = options.headers;
      return {
        ok: true,
        async json() {
          return {
            response: [
              {
                fixture: {
                  id: 123,
                  date: "2026-06-09T19:00:00+00:00",
                  status: { short: "FT" },
                },
                league: { name: "Friendly", season: 2026, round: "Round 1" },
                teams: {
                  home: { id: 26, name: "Argentina" },
                  away: { id: 2, name: "France" },
                },
                goals: { home: 2, away: 1 },
              },
              {
                fixture: {
                  id: 124,
                  date: "2026-06-09T21:00:00+00:00",
                  status: { short: "NS" },
                },
                teams: {
                  home: { id: 1, name: "A" },
                  away: { id: 2, name: "B" },
                },
                goals: { home: null, away: null },
              },
            ],
          };
        },
      };
    },
  });

  const matches = await provider.get_completed_matches("2026-06-09");

  assert.equal(matches.length, 1);
  assert.equal(matches[0].providerFixtureId, 123);
  assert.equal(matches[0].homeTeam.name, "Argentina");
  assert.equal(requestedUrl.searchParams.get("date"), "2026-06-09");
  assert.equal(requestedHeaders["x-apisports-key"], "test-key");
});

test("fixture detail methods use their dedicated API-Football endpoints", async () => {
  const paths = [];
  const provider = new ApiFootballProvider({
    apiKey: "test-key",
    logger: silentLogger,
    fetchImpl: async (url) => {
      paths.push(`${url.pathname}?${url.searchParams}`);
      return {
        ok: true,
        async json() {
          return { response: [] };
        },
      };
    },
  });

  await provider.get_fixture_statistics(123);
  await provider.get_fixture_players(123);
  await provider.get_lineups(123);

  assert.deepEqual(paths, [
    "/fixtures/statistics?fixture=123",
    "/fixtures/players?fixture=123",
    "/fixtures/lineups?fixture=123",
  ]);
});

test("sample provider exposes all requested ingestion methods", async () => {
  const provider = new SampleSportsProvider({ logger: silentLogger });
  const matches = await provider.get_completed_matches("2026-06-09");
  const fixtureId = matches[0].providerFixtureId;

  assert.equal(matches.length, 1);
  assert.equal((await provider.get_fixture_statistics(fixtureId)).length, 2);
  assert.equal((await provider.get_fixture_players(fixtureId)).length, 2);
  assert.equal((await provider.get_lineups(fixtureId)).length, 2);
});
