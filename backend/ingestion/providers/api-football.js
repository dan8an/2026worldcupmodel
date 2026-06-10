import { SportsProvider } from "./sports-provider.js";

const COMPLETED_STATUSES = new Set(["FT", "AET", "PEN"]);

const normalizeTeam = (team = {}) => ({
  providerId: Number(team.id),
  name: team.name,
});

const normalizeFixture = (item) => ({
  providerFixtureId: Number(item.fixture.id),
  date: item.fixture.date,
  status: item.fixture.status?.short,
  competition: item.league?.name ?? null,
  season: item.league?.season ?? null,
  round: item.league?.round ?? null,
  homeTeam: normalizeTeam(item.teams?.home),
  awayTeam: normalizeTeam(item.teams?.away),
  homeScore: item.goals?.home ?? null,
  awayScore: item.goals?.away ?? null,
  raw: item,
});

const normalizeStatistics = (item) => ({
  team: normalizeTeam(item.team),
  statistics: Object.fromEntries(
    (item.statistics ?? []).map((stat) => [stat.type, stat.value]),
  ),
  raw: item,
});

const normalizePlayer = (item, team) => {
  const statistics = item.statistics?.[0] ?? {};
  return {
    team,
    player: {
      providerId: Number(item.player?.id),
      name: item.player?.name,
      photo: item.player?.photo ?? null,
    },
    appearance: {
      minutes: statistics.games?.minutes ?? null,
      position: statistics.games?.position ?? null,
      rating: statistics.games?.rating == null
        ? null
        : Number(statistics.games.rating),
      captain: Boolean(statistics.games?.captain),
      substitute: Boolean(statistics.games?.substitute),
      shots: statistics.shots?.total ?? null,
      shotsOnTarget: statistics.shots?.on ?? null,
      goals: statistics.goals?.total ?? null,
      assists: statistics.goals?.assists ?? null,
      saves: statistics.goals?.saves ?? null,
      keyPasses: statistics.passes?.key ?? null,
      tackles: statistics.tackles?.total ?? null,
      interceptions: statistics.tackles?.interceptions ?? null,
      yellowCards: statistics.cards?.yellow ?? null,
      redCards: statistics.cards?.red ?? null,
    },
    raw: item,
  };
};

const normalizeLineupPlayer = (item) => ({
  providerId: Number(item.player?.id),
  name: item.player?.name,
  position: item.player?.pos ?? null,
  grid: item.player?.grid ?? null,
});

export class ApiFootballProvider extends SportsProvider {
  constructor({
    apiKey,
    baseUrl = "https://v3.football.api-sports.io",
    fetchImpl = globalThis.fetch,
    logger = console,
  }) {
    super("api_football");
    if (!apiKey) throw new Error("API_FOOTBALL_KEY is required");
    if (!fetchImpl) throw new Error("A fetch implementation is required");
    this.apiKey = apiKey;
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.fetch = fetchImpl;
    this.logger = logger;
  }

  async request(path, params) {
    const url = new URL(`${this.baseUrl}${path}`);
    for (const [key, value] of Object.entries(params)) {
      url.searchParams.set(key, String(value));
    }

    this.logger.info(`[sports:api_football] GET ${url.pathname}${url.search}`);
    const response = await this.fetch(url, {
      headers: { "x-apisports-key": this.apiKey },
    });
    if (!response.ok) {
      throw new Error(
        `API-Football request failed (${response.status} ${response.statusText})`,
      );
    }

    const payload = await response.json();
    const errors = payload.errors &&
      (Array.isArray(payload.errors)
        ? payload.errors
        : Object.values(payload.errors));
    if (errors?.length) {
      throw new Error(`API-Football error: ${errors.join("; ")}`);
    }
    return payload.response ?? [];
  }

  async get_completed_matches(date) {
    const response = await this.request("/fixtures", { date });
    return response
      .filter((item) => COMPLETED_STATUSES.has(item.fixture?.status?.short))
      .map(normalizeFixture);
  }

  async get_fixture_statistics(fixtureId) {
    const response = await this.request("/fixtures/statistics", {
      fixture: fixtureId,
    });
    return response.map(normalizeStatistics);
  }

  async get_fixture_players(fixtureId) {
    const response = await this.request("/fixtures/players", {
      fixture: fixtureId,
    });
    return response.flatMap((teamEntry) => {
      const team = normalizeTeam(teamEntry.team);
      return (teamEntry.players ?? []).map((item) =>
        normalizePlayer(item, team)
      );
    });
  }

  async get_lineups(fixtureId) {
    const response = await this.request("/fixtures/lineups", {
      fixture: fixtureId,
    });
    return response.map((item) => ({
      team: normalizeTeam(item.team),
      formation: item.formation ?? null,
      starters: (item.startXI ?? []).map(normalizeLineupPlayer),
      substitutes: (item.substitutes ?? []).map(normalizeLineupPlayer),
      raw: item,
    }));
  }
}
