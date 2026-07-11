import { readFileSync } from "node:fs";

const readJson = (relativePath, fallback) => {
  try {
    return JSON.parse(readFileSync(new URL(relativePath, import.meta.url), "utf8"));
  } catch {
    return fallback;
  }
};

const seedTeams = readJson("../data/seed/teams.json", []);
const generatedSnapshot = readJson("../data/generated/latest.json", {});

const FLAG_CODES = {
  MEX: "MX", RSA: "ZA", KOR: "KR", CZE: "CZ",
  CAN: "CA", BIH: "BA", QAT: "QA", SUI: "CH",
  BRA: "BR", MAR: "MA", HAI: "HT", SCO: "GB-SCT",
  USA: "US", PAR: "PY", AUS: "AU", TUR: "TR",
  GER: "DE", CUW: "CW", CIV: "CI", ECU: "EC",
  NED: "NL", JPN: "JP", SWE: "SE", TUN: "TN",
  BEL: "BE", EGY: "EG", IRN: "IR", NZL: "NZ",
  ESP: "ES", CPV: "CV", KSA: "SA", URU: "UY",
  FRA: "FR", SEN: "SN", IRQ: "IQ", NOR: "NO",
  ARG: "AR", ALG: "DZ", AUT: "AT", JOR: "JO",
  POR: "PT", COD: "CD", UZB: "UZ", COL: "CO",
  ENG: "GB-ENG", CRO: "HR", GHA: "GH", PAN: "PA",
};

const SUBDIVISION_FLAGS = {
  "GB-ENG": "\u{1F3F4}\u{E0067}\u{E0062}\u{E0065}\u{E006E}\u{E0067}\u{E007F}",
  "GB-SCT": "\u{1F3F4}\u{E0067}\u{E0062}\u{E0073}\u{E0063}\u{E0074}\u{E007F}",
};

const CONFEDERATIONS = {
  MEX: "CONCACAF", RSA: "CAF", KOR: "AFC", CZE: "UEFA",
  CAN: "CONCACAF", BIH: "UEFA", QAT: "AFC", SUI: "UEFA",
  BRA: "CONMEBOL", MAR: "CAF", HAI: "CONCACAF", SCO: "UEFA",
  USA: "CONCACAF", PAR: "CONMEBOL", AUS: "AFC", TUR: "UEFA",
  GER: "UEFA", CUW: "CONCACAF", CIV: "CAF", ECU: "CONMEBOL",
  NED: "UEFA", JPN: "AFC", SWE: "UEFA", TUN: "CAF",
  BEL: "UEFA", EGY: "CAF", IRN: "AFC", NZL: "OFC",
  ESP: "UEFA", CPV: "CAF", KSA: "AFC", URU: "CONMEBOL",
  FRA: "UEFA", SEN: "CAF", IRQ: "AFC", NOR: "UEFA",
  ARG: "CONMEBOL", ALG: "CAF", AUT: "UEFA", JOR: "AFC",
  POR: "UEFA", COD: "CAF", UZB: "AFC", COL: "CONMEBOL",
  ENG: "UEFA", CRO: "UEFA", GHA: "CAF", PAN: "CONCACAF",
};

const GROUP_MATCH_DATES = {
  A: [11, 18, 24], B: [12, 18, 24], C: [13, 19, 24],
  D: [12, 19, 25], E: [14, 20, 25], F: [14, 20, 25],
  G: [15, 21, 26], H: [15, 21, 26], I: [16, 22, 26],
  J: [16, 22, 27], K: [17, 23, 27], L: [17, 23, 27],
};

const GROUP_PAIRINGS = [
  [[1, 2], [3, 4]],
  [[1, 3], [4, 2]],
  [[4, 1], [2, 3]],
];

const VENUE_IDS = [
  "MEX", "TOR", "LA", "BOS", "SF", "DAL", "HOU", "KC",
  "ATL", "MIA", "NYNJ", "PHI", "SEA", "VAN", "GDL", "MTY",
];

const normalizeName = (value = "") =>
  value.toLowerCase().replace(/[^a-z0-9]/g, "");

const jsonValue = (value, fallback) => {
  if (value == null) return fallback;
  if (typeof value !== "string") return value;
  try {
    return JSON.parse(value);
  } catch {
    return fallback;
  }
};

const numberOrNull = (value) =>
  value == null || value === "" ? null : Number(value);

const stageFromValue = (value) => {
  const raw = String(value ?? "").toLowerCase();
  if (raw.includes("round of 32") || raw.includes("round_of_32")) return "round_of_32";
  if (raw.includes("round of 16") || raw.includes("round_of_16")) return "round_of_16";
  if (raw.includes("quarter")) return "quarterfinal";
  if (raw.includes("semi")) return "semifinal";
  if (raw.includes("third") || raw.includes("3rd")) return "third_place";
  if (raw.includes("final")) return "final";
  if (raw.includes("group")) return "group";
  return value ?? "group";
};

const KNOCKOUT_STAGES = new Set([
  "round_of_32",
  "round_of_16",
  "quarterfinal",
  "semifinal",
  "third_place",
  "final",
]);

const KNOCKOUT_STAGE_LIMITS = {
  round_of_32: 16,
  round_of_16: 8,
  quarterfinal: 4,
  semifinal: 2,
  final: 1,
  third_place: 1,
};

const KNOCKOUT_MATCH_NUMBER_RANGES = {
  round_of_32: [73, 88],
  round_of_16: [89, 96],
  quarterfinal: [97, 100],
  semifinal: [101, 102],
  third_place: [103, 103],
  final: [104, 104],
};

const COMPLETED_MATCH_STATUSES = new Set(["completed", "finished", "ft", "aet", "pen"]);
const KNOCKOUT_WINDOW_START = Date.parse("2026-06-28T00:00:00Z");
const KNOCKOUT_WINDOW_END = Date.parse("2026-07-20T00:00:00Z");

const friendlySlot = (slot) => {
  if (slot == null || slot === "") return "TBD";
  const raw = String(slot).trim();
  if (/^\d+$/.test(raw)) return "TBD";
  return raw;
};

const officialMatchNumber = (match) => {
  const number = Number(match.number);
  const range = KNOCKOUT_MATCH_NUMBER_RANGES[match.stage];
  if (!Number.isFinite(number) || !range) return null;
  return number >= range[0] && number <= range[1] ? number : null;
};

const providerPayload = (match) =>
  jsonValue(match.provider_payload ?? match.raw_payload ?? match.raw, {});

const isWorldCup2026ProviderMatch = (match) => {
  const payload = providerPayload(match);
  const league = payload?.league ?? {};
  return String(match.provider_name ?? "").toLowerCase() === "api_football" &&
    String(league.id ?? "") === "1" &&
    String(league.season ?? "") === "2026";
};

const hasScore = (match) => match.home_score != null && match.away_score != null;

const isCompletedMatch = (match) =>
  Boolean(match.completed) ||
  COMPLETED_MATCH_STATUSES.has(String(match.status ?? "").toLowerCase()) ||
  hasScore(match);

const knockoutSortTime = (match) => {
  const value = Date.parse(match.kickoff);
  return Number.isNaN(value) ? Number.POSITIVE_INFINITY : value;
};

const knockoutLogicalKey = (match, officialNumber) => {
  if (officialNumber != null) return `number:${match.stage}:${officialNumber}`;
  if (match.provider_fixture_id != null) return `provider:${match.provider_fixture_id}`;
  return [
    "teams",
    match.stage,
    match.kickoff,
    match.home_team?.id,
    match.away_team?.id,
  ].join(":");
};

const knockoutRank = (match, officialNumber) => [
  isCompletedMatch(match) ? 1 : 0,
  hasScore(match) ? 1 : 0,
  officialNumber != null ? 1 : 0,
  Date.parse(match.updated_at ?? match.created_at ?? "") || 0,
];

const compareRank = (left, right) => {
  for (let index = 0; index < left.length; index += 1) {
    if (left[index] !== right[index]) return left[index] - right[index];
  }
  return 0;
};

export const officialKnockoutMatches = (matches) => {
  const selected = new Map();
  matches.forEach((match) => {
    if (!KNOCKOUT_STAGES.has(match.stage)) return;
    if (!match.home_team || !match.away_team) return;
    const kickoff = knockoutSortTime(match);
    if (kickoff < KNOCKOUT_WINDOW_START || kickoff >= KNOCKOUT_WINDOW_END) return;
    const number = officialMatchNumber(match);
    if (number == null && !isWorldCup2026ProviderMatch(match)) return;
    const key = knockoutLogicalKey(match, number);
    const ranked = { match, number, rank: knockoutRank(match, number) };
    const current = selected.get(key);
    if (!current || compareRank(ranked.rank, current.rank) > 0) {
      selected.set(key, ranked);
    }
  });

  return Object.entries(KNOCKOUT_STAGE_LIMITS).flatMap(([stage, limit]) =>
    [...selected.values()]
      .filter((item) => item.match.stage === stage)
      .sort((left, right) =>
        (left.number == null) - (right.number == null) ||
        (left.number ?? 999) - (right.number ?? 999) ||
        knockoutSortTime(left.match) - knockoutSortTime(right.match)
      )
      .slice(0, limit)
      .map(({ match, number }) => ({
        ...match,
        number: number ?? 0,
      }))
  ).sort((left, right) =>
    (left.number === 0) - (right.number === 0) ||
    (left.number || 999) - (right.number || 999) ||
    knockoutSortTime(left) - knockoutSortTime(right)
  );
};

const explanationFields = (prediction = {}) => ({
  elo_base_home_probability: numberOrNull(
    prediction.elo_base_home_probability,
  ),
  elo_base_draw_probability: numberOrNull(
    prediction.elo_base_draw_probability,
  ),
  elo_base_away_probability: numberOrNull(
    prediction.elo_base_away_probability,
  ),
  attack_defense_adjustment: numberOrNull(
    prediction.attack_defense_adjustment,
  ),
  draw_calibration_adjustment: numberOrNull(
    prediction.draw_calibration_adjustment,
  ),
  context_adjustment_total: numberOrNull(
    prediction.context_adjustment_total,
  ),
  final_home_probability: Number(
    prediction.final_home_probability ??
      prediction.home_win_probability ??
      0,
  ),
  final_draw_probability: Number(
    prediction.final_draw_probability ??
      prediction.draw_probability ??
      0,
  ),
  final_away_probability: Number(
    prediction.final_away_probability ??
      prediction.away_win_probability ??
      0,
  ),
  confidence_score: numberOrNull(prediction.confidence_score),
  confidence_tier: prediction.confidence_tier ?? null,
  confidence_explanation: prediction.confidence_explanation ?? null,
  top_factors: jsonValue(prediction.top_factors, []),
});

const seedByName = new Map(
  seedTeams.map((team) => [normalizeName(team.name), team]),
);

const flagForTeam = (teamId) => {
  const code = FLAG_CODES[teamId];
  if (!code) return "";
  if (SUBDIVISION_FLAGS[code]) return SUBDIVISION_FLAGS[code];
  return [...code].map((letter) =>
    String.fromCodePoint(0x1f1e6 + letter.charCodeAt(0) - 65)
  ).join("");
};

export const normalizeTeam = (row = {}) => {
  const seed = seedByName.get(normalizeName(row.name)) ??
    seedTeams.find((team) => team.id === row.id) ??
    row;
  const rank = Number(row.fifa_rank ?? row.rank ?? seed.rank ?? 100);

  return {
    id: seed.id ?? String(row.id),
    name: row.name ?? seed.name ?? "Unknown team",
    group: row.group_code ?? row.group ?? seed.group ?? "",
    position: Number(row.group_position ?? row.position ?? seed.position ?? 0),
    rank,
    host: Boolean(row.is_host ?? row.host ?? seed.host ?? false),
    elo: Number(row.elo_rating ?? row.elo ?? 2100 - rank * 10),
    flag: flagForTeam(seed.id ?? row.id),
    confederation: row.confederation ??
      CONFEDERATIONS[seed.id ?? row.id] ??
      "",
  };
};

export const mergeTeams = (databaseRows = []) => {
  const databaseByName = new Map(
    databaseRows.map((row) => [normalizeName(row.name), row]),
  );

  return seedTeams.map((seed) =>
    normalizeTeam({ ...seed, ...databaseByName.get(normalizeName(seed.name)) })
  );
};

const predictionByMatchId = new Map(
  (generatedSnapshot.predictions ?? []).map((prediction) => [
    prediction.match_id,
    {
      ...prediction,
      model_version: generatedSnapshot.model_version ?? "placeholder",
      generated_at: generatedSnapshot.generated_at ?? new Date(0).toISOString(),
      data_cutoff: generatedSnapshot.data_cutoff ?? new Date(0).toISOString(),
    },
  ]),
);

export const buildPlaceholderMatches = (teams = mergeTeams()) => {
  const teamsByGroupPosition = new Map(
    teams.map((team) => [`${team.group}-${team.position}`, team]),
  );
  const fixtures = [];
  let number = 1;

  for (const group of Object.keys(GROUP_MATCH_DATES)) {
    GROUP_PAIRINGS.forEach((pairings, matchday) => {
      pairings.forEach(([homePosition, awayPosition], index) => {
        const kickoff = new Date(Date.UTC(
          2026,
          5,
          GROUP_MATCH_DATES[group][matchday],
          17 + index * 3,
        ));
        const id = `WC26-${String(number).padStart(3, "0")}`;

        fixtures.push({
          id,
          number,
          stage: "group",
          kickoff: kickoff.toISOString(),
          venue_id: VENUE_IDS[(number - 1) % VENUE_IDS.length],
          group,
          home_team: teamsByGroupPosition.get(`${group}-${homePosition}`) ?? null,
          away_team: teamsByGroupPosition.get(`${group}-${awayPosition}`) ?? null,
          home_slot: null,
          away_slot: null,
          status: "scheduled",
          home_score: null,
          away_score: null,
          prediction: predictionByMatchId.get(id) ?? null,
        });
        number += 1;
      });
    });
  }

  return fixtures.sort((a, b) =>
    a.kickoff.localeCompare(b.kickoff) || a.number - b.number
  );
};

const resolveTeam = (value, teams, databaseRows) => {
  if (value == null) return null;
  const rawTeam = databaseRows.find((row) =>
    String(row.id) === String(value) || normalizeName(row.name) === normalizeName(value)
  );
  const canonicalId = rawTeam ? normalizeTeam(rawTeam).id : String(value);
  return teams.find((team) =>
    team.id === canonicalId || normalizeName(team.name) === normalizeName(value)
  ) ?? null;
};

const predictionTime = (prediction) => {
  const value = prediction.prediction_timestamp ?? prediction.created_at ??
    prediction.generated_at;
  const timestamp = value ? new Date(value).getTime() : Number.NaN;
  return Number.isFinite(timestamp) ? timestamp : null;
};

const resolveKnockoutPrediction = (
  row,
  predictionRows,
  matchId,
  matchNumber,
  kickoff,
  homeTeam,
  awayTeam,
) => {
  const kickoffTime = new Date(kickoff).getTime();
  const providerId = row.api_football_fixture_id ?? row.provider_fixture_id;
  const canonicalId = row.canonical_match_id;
  const officialId = matchNumber >= 73 && matchNumber <= 104
    ? `WC26-${String(matchNumber).padStart(3, "0")}`
    : null;
  const candidates = [];

  predictionRows.forEach((prediction) => {
    const timestamp = predictionTime(prediction);
    if (timestamp == null || timestamp > kickoffTime) return;
    const predictionProvider = prediction.provider_fixture_id ??
      prediction.api_football_fixture_id;
    const predictionNumber = Number(
      prediction.match_number ?? prediction.number ?? Number.NaN,
    );
    let priority = null;
    if (String(prediction.match_id ?? "") === String(matchId)) {
      priority = 0;
    } else if (
      providerId != null && predictionProvider != null &&
      String(providerId) === String(predictionProvider)
    ) {
      priority = 1;
    } else if (
      (canonicalId != null &&
        String(prediction.canonical_match_id ?? "") === String(canonicalId)) ||
      (officialId != null &&
        String(prediction.canonical_match_id ?? "") === officialId) ||
      (Number.isFinite(predictionNumber) && predictionNumber === matchNumber)
    ) {
      priority = 2;
    } else {
      const predictionKickoff = prediction.kickoff ?? prediction.match_date;
      const predictionKickoffTime = predictionKickoff
        ? new Date(predictionKickoff).getTime()
        : Number.NaN;
      const predictionHome = prediction.home_team_id ?? prediction.home_team;
      const predictionAway = prediction.away_team_id ?? prediction.away_team;
      if (
        homeTeam && awayTeam && Number.isFinite(predictionKickoffTime) &&
        (String(predictionHome) === homeTeam.id ||
          normalizeName(predictionHome) === normalizeName(homeTeam.name)) &&
        (String(predictionAway) === awayTeam.id ||
          normalizeName(predictionAway) === normalizeName(awayTeam.name)) &&
        Math.abs(predictionKickoffTime - kickoffTime) <= 3 * 60 * 60 * 1000
      ) {
        priority = 3;
      }
    }
    if (priority != null) candidates.push({ priority, timestamp, prediction });
  });

  candidates.sort((left, right) =>
    left.priority - right.priority || right.timestamp - left.timestamp
  );
  return candidates[0]?.prediction ?? null;
};

export const normalizeDatabaseMatches = (
  matchRows,
  predictionRows,
  teams,
  databaseTeamRows,
) => {
  const predictionsByMatchId = new Map();
  predictionRows.forEach((prediction) => {
    const matchId = String(
      prediction.canonical_match_id ?? prediction.match_id,
    );
    if (!predictionsByMatchId.has(matchId)) {
      predictionsByMatchId.set(matchId, prediction);
    }
  });

  return matchRows.map((row, index) => {
    const stage = stageFromValue(row.stage ?? row.tournament_stage ?? "group");
    const canonicalId = stage === "group"
      ? row.canonical_match_id ?? row.match_id
      : row.id ?? row.api_football_fixture_id ?? row.provider_fixture_id ?? row.canonical_match_id ?? row.match_id;
    const matchNumber = Number(
      row.match_number ?? row.number ?? String(canonicalId ?? row.id).match(/\d+$/)?.[0] ?? index + 1,
    );
    const id = String(canonicalId ?? row.id);
    const homeTeam = resolveTeam(
      row.home_team_id ?? row.home_team,
      teams,
      databaseTeamRows,
    );
    const awayTeam = resolveTeam(
      row.away_team_id ?? row.away_team,
      teams,
      databaseTeamRows,
    );
    const databasePrediction = stage === "group"
      ? predictionsByMatchId.get(id)
      : resolveKnockoutPrediction(
        row,
        predictionRows,
        id,
        matchNumber,
        row.kickoff ?? row.match_date,
        homeTeam,
        awayTeam,
      );
    const snapshotPrediction = predictionByMatchId.get(id);
    const prediction = databasePrediction
      ? {
          match_id: id,
          home_team_id: homeTeam?.id ?? "",
          away_team_id: awayTeam?.id ?? "",
          home_xg: Number(
            databasePrediction.home_xg ?? snapshotPrediction?.home_xg ?? 0,
          ),
          away_xg: Number(
            databasePrediction.away_xg ?? snapshotPrediction?.away_xg ?? 0,
          ),
          probabilities: {
            home_win: Number(
              databasePrediction.home_win ??
                databasePrediction.home_win_probability ??
                databasePrediction.home_win_prob ??
                snapshotPrediction?.probabilities.home_win ??
                0,
            ),
            draw: Number(
              databasePrediction.draw ??
                databasePrediction.draw_probability ??
                databasePrediction.draw_prob ??
                snapshotPrediction?.probabilities.draw ??
                0,
            ),
            away_win: Number(
              databasePrediction.away_win ??
                databasePrediction.away_win_probability ??
                databasePrediction.away_win_prob ??
                snapshotPrediction?.probabilities.away_win ??
                0,
            ),
          },
          ...explanationFields(databasePrediction),
          top_scores: snapshotPrediction?.top_scores ?? [],
          confidence: databasePrediction.confidence_tier ??
            snapshotPrediction?.confidence ??
            "High uncertainty",
          key_factors: databasePrediction.explanation_factors ??
            snapshotPrediction?.key_factors ??
            [],
          context: snapshotPrediction?.context ?? emptyPredictionContext(),
          model_version: databasePrediction.model_version ?? "supabase",
          generated_at: databasePrediction.prediction_timestamp ??
            databasePrediction.created_at ??
            new Date().toISOString(),
          data_cutoff: databasePrediction.data_cutoff ??
            databasePrediction.created_at ??
            new Date().toISOString(),
        }
      : snapshotPrediction ?? null;

    return {
      id,
      number: matchNumber,
      provider_fixture_id: row.api_football_fixture_id ?? row.provider_fixture_id ?? null,
      provider_name: row.provider_name ?? null,
      provider_payload: row.provider_payload ?? row.raw_payload ?? row.raw ?? null,
      updated_at: row.updated_at ?? row.created_at ?? null,
      stage,
      kickoff: new Date(row.kickoff ?? row.match_date).toISOString(),
      venue_id: row.venue_id ?? "TBD",
      group: row.group_code ?? row.group ?? homeTeam?.group ?? null,
      home_team: homeTeam,
      away_team: awayTeam,
      home_slot: homeTeam ? null : friendlySlot(row.home_slot ?? row.home_team ?? null),
      away_slot: awayTeam ? null : friendlySlot(row.away_slot ?? row.away_team ?? null),
      status: row.status ?? (row.completed ? "completed" : "scheduled"),
      home_score: row.home_score ?? null,
      away_score: row.away_score ?? null,
      prediction,
    };
  }).sort((a, b) =>
    a.kickoff.localeCompare(b.kickoff) || a.number - b.number
  );
};

const hoursBetween = (left, right) =>
  Math.abs(new Date(left).getTime() - new Date(right).getTime()) / (60 * 60 * 1000);

const matchesCanonicalFixture = (canonical, databaseMatch) => {
  if (canonical.id === databaseMatch.id) return true;
  if (!canonical.home_team || !canonical.away_team) return false;
  return databaseMatch.home_team?.id === canonical.home_team.id &&
    databaseMatch.away_team?.id === canonical.away_team.id &&
    hoursBetween(databaseMatch.kickoff, canonical.kickoff) <= 36;
};

export const mergeDatabaseMatches = (canonicalMatches, databaseMatches) => {
  const merged = canonicalMatches.map((canonical) => {
    const databaseMatch = databaseMatches.find((candidate) =>
      matchesCanonicalFixture(canonical, candidate)
    );
    if (!databaseMatch) return canonical;

    const prediction = databaseMatch.prediction
      ? {
          ...canonical.prediction,
          ...databaseMatch.prediction,
          match_id: canonical.id,
          home_team_id: canonical.home_team?.id ?? "",
          away_team_id: canonical.away_team?.id ?? "",
          top_scores: databaseMatch.prediction.top_scores.length
            ? databaseMatch.prediction.top_scores
            : canonical.prediction?.top_scores ?? [],
          key_factors: databaseMatch.prediction.key_factors.length
            ? databaseMatch.prediction.key_factors
            : canonical.prediction?.key_factors ?? [],
          context: {
            ...canonical.prediction?.context,
            ...databaseMatch.prediction.context,
          },
        }
      : canonical.prediction;

    return {
      ...canonical,
      kickoff: databaseMatch.kickoff,
      venue_id: databaseMatch.venue_id === "TBD"
        ? canonical.venue_id
        : databaseMatch.venue_id || canonical.venue_id,
      home_team: databaseMatch.home_team ?? canonical.home_team,
      away_team: databaseMatch.away_team ?? canonical.away_team,
      home_slot: databaseMatch.home_team ? null : databaseMatch.home_slot ?? canonical.home_slot,
      away_slot: databaseMatch.away_team ? null : databaseMatch.away_slot ?? canonical.away_slot,
      status: databaseMatch.status ?? canonical.status,
      home_score: databaseMatch.home_score ?? canonical.home_score ?? null,
      away_score: databaseMatch.away_score ?? canonical.away_score ?? null,
      prediction,
    };
  });
  const usedIds = new Set(merged.map((match) => match.id));
  const providerKnockouts = officialKnockoutMatches(
    databaseMatches.filter((match) => !usedIds.has(match.id)),
  );
  return [...merged, ...providerKnockouts].sort((a, b) =>
    a.kickoff.localeCompare(b.kickoff) || a.number - b.number
  );
};

export const mergeCanonicalPredictions = (matches, predictionRows = []) => {
  const latestByCanonicalId = new Map();
  predictionRows.forEach((prediction) => {
    const canonicalId = prediction.canonical_match_id;
    if (canonicalId && !latestByCanonicalId.has(String(canonicalId))) {
      latestByCanonicalId.set(String(canonicalId), prediction);
    }
  });

  return matches.map((match) => {
    const prediction = latestByCanonicalId.get(match.id);
    if (!prediction) return match;
    return {
      ...match,
      prediction: {
        ...match.prediction,
        match_id: match.id,
        home_team_id: match.home_team?.id ?? "",
        away_team_id: match.away_team?.id ?? "",
        home_xg: Number(prediction.home_xg ?? match.prediction?.home_xg ?? 0),
        away_xg: Number(prediction.away_xg ?? match.prediction?.away_xg ?? 0),
        probabilities: {
          home_win: Number(prediction.home_win_probability ?? 0),
          draw: Number(prediction.draw_probability ?? 0),
          away_win: Number(prediction.away_win_probability ?? 0),
        },
        ...explanationFields(prediction),
        model_version: prediction.model_version ?? "supabase",
        generated_at: prediction.prediction_timestamp ??
          prediction.created_at ??
          new Date().toISOString(),
        data_cutoff: prediction.data_cutoff ??
          prediction.prediction_timestamp ??
          new Date().toISOString(),
      },
    };
  });
};

const emptyPredictionContext = () => ({
  home_form_elo: 0,
  away_form_elo: 0,
  home_h2h_elo: 0,
  away_h2h_elo: 0,
  home_availability_elo: 0,
  away_availability_elo: 0,
  historical_matches_home: 0,
  historical_matches_away: 0,
  h2h_matches: 0,
  availability_reports: 0,
  data_cutoff: null,
});

export const snapshotSimulation = () => {
  if (!generatedSnapshot.simulation?.teams?.length) return null;
  const generatedAt = generatedSnapshot.generated_at ??
    new Date(0).toISOString();
  return {
    ...generatedSnapshot.simulation,
    model_version: generatedSnapshot.model_version ?? "placeholder",
    generated_at: generatedAt,
    created_at: generatedAt,
    data_cutoff: generatedSnapshot.data_cutoff ?? new Date(0).toISOString(),
    source: "fallback_static",
  };
};

export const normalizeDatabaseSimulation = (run, probabilities, teams) => ({
  iterations: Number(run.num_simulations ?? run.iterations),
  seed: Number(run.random_seed ?? run.seed ?? 2026),
  model_version: run.model_version ?? run.semantic_version ?? "supabase",
  generated_at: run.generated_at ?? run.created_at,
  created_at: run.created_at ?? run.generated_at,
  data_cutoff: run.data_cutoff ?? run.generated_at ?? run.created_at,
  source: "database_latest",
  monte_carlo_precision: {
    worst_case_standard_error: Math.sqrt(
      0.25 / Number(run.num_simulations ?? run.iterations),
    ),
    worst_case_95_margin: 1.96 * Math.sqrt(
      0.25 / Number(run.num_simulations ?? run.iterations),
    ),
  },
  teams: probabilities.map((row) => {
    const team = resolveTeam(
      row.team_id ?? row.team_name,
      teams,
      teams,
    );
    return {
      team_id: team?.id ?? String(row.team_id),
      team_name: team?.name ?? row.team_name ?? "Unknown team",
      flag: team?.flag ?? "",
      group: team?.group ?? "",
      confederation: team?.confederation ?? "",
      group_stage_exit: Number(
        row.group_stage_exit_probability ?? row.group_stage_exit ?? 0,
      ),
      round_of_32: Number(
        row.round_of_32_probability ?? row.round_of_32,
      ),
      round_of_16: Number(
        row.round_of_16_probability ?? row.round_of_16,
      ),
      quarterfinal: Number(
        row.quarterfinal_probability ?? row.quarterfinal,
      ),
      semifinal: Number(row.semifinal_probability ?? row.semifinal),
      final: Number(row.final_probability ?? row.final),
      champion: Number(row.champion_probability ?? row.champion),
    };
  }),
});
