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

const KNOCKOUT_ROUNDS = [
  ["round_of_32", 16, Date.UTC(2026, 5, 28, 16)],
  ["round_of_16", 8, Date.UTC(2026, 6, 4, 16)],
  ["quarterfinal", 4, Date.UTC(2026, 6, 9, 19)],
  ["semifinal", 2, Date.UTC(2026, 6, 14, 19)],
  ["third_place", 1, Date.UTC(2026, 6, 18, 19)],
  ["final", 1, Date.UTC(2026, 6, 19, 19)],
];

const COMPLETED_STATUSES = new Set([
  "completed",
  "finished",
  "full_time",
  "ft",
  "aet",
  "pen",
]);

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

const knockoutRoundName = (number) => {
  if (number <= 88) return ["Round of 32", number - 72];
  if (number <= 96) return ["Round of 16", number - 88];
  if (number <= 100) return ["Quarterfinal", number - 96];
  if (number <= 102) return ["Semifinal", number - 100];
  if (number === 103) return ["Third-place Match", 1];
  return ["Final", 1];
};

const friendlySlot = (slot) => {
  if (slot == null || slot === "") return "TBD";
  const raw = String(slot).trim();
  if (/^\d+$/.test(raw)) return "TBD";

  const groupSlot = raw.match(/^(Winner|Runner-up|Third(?:-place)?) Group ([A-L])$/i);
  if (groupSlot) return `${groupSlot[1]} Group ${groupSlot[2].toUpperCase()}`;

  const matchSlot = raw.match(/^(Winner|Loser) M(?:atch )?(\d+)$/i);
  if (matchSlot) {
    const [, outcome, rawNumber] = matchSlot;
    const [round, index] = knockoutRoundName(Number(rawNumber));
    return `${outcome} ${round}${round === "Final" || round === "Third-place Match" ? "" : ` Match ${index}`}`;
  }

  const r32Slot = raw.match(/^R32-([HA])(\d+)$/i);
  if (r32Slot) {
    const side = r32Slot[1].toUpperCase() === "H" ? "home" : "away";
    return `Round of 32 ${side} qualifier ${Number(r32Slot[2])}`;
  }

  return raw;
};

const isCompletedMatch = (match) =>
  match.completed === true ||
  COMPLETED_STATUSES.has(String(match.status ?? "").toLowerCase()) ||
  (match.home_score != null && match.away_score != null);

const winnerTeam = (match) => {
  if (!isCompletedMatch(match)) return null;
  if (match.winner_team) return match.winner_team;
  if (!match.home_team || !match.away_team) return null;
  const homeScore = numberOrNull(match.home_score);
  const awayScore = numberOrNull(match.away_score);
  if (homeScore == null || awayScore == null || homeScore === awayScore) return null;
  return homeScore > awayScore ? match.home_team : match.away_team;
};

const loserTeam = (match) => {
  if (!isCompletedMatch(match)) return null;
  if (!match.home_team || !match.away_team) return null;
  const winner = winnerTeam(match);
  if (!winner) return null;
  return winner.id === match.home_team.id ? match.away_team : match.home_team;
};

const rankGroup = (teams, groupMatches) => {
  const rows = new Map(
    teams.map((team) => [team.id, {
      team,
      points: 0,
      goals_for: 0,
      goals_against: 0,
    }]),
  );
  groupMatches.forEach((match) => {
    if (!isCompletedMatch(match) || !match.home_team || !match.away_team) return;
    const home = rows.get(match.home_team.id);
    const away = rows.get(match.away_team.id);
    const homeScore = numberOrNull(match.home_score);
    const awayScore = numberOrNull(match.away_score);
    if (!home || !away || homeScore == null || awayScore == null) return;
    home.goals_for += homeScore;
    home.goals_against += awayScore;
    away.goals_for += awayScore;
    away.goals_against += homeScore;
    if (homeScore > awayScore) home.points += 3;
    else if (awayScore > homeScore) away.points += 3;
    else {
      home.points += 1;
      away.points += 1;
    }
  });
  return [...rows.values()].sort((left, right) =>
    right.points - left.points ||
    (right.goals_for - right.goals_against) - (left.goals_for - left.goals_against) ||
    right.goals_for - left.goals_for ||
    left.team.id.localeCompare(right.team.id)
  );
};

const qualificationKey = (row) => [
  -row.points,
  -(row.goals_for - row.goals_against),
  -row.goals_for,
  row.team.id,
];

const compareQualification = (left, right) => {
  const a = qualificationKey(left);
  const b = qualificationKey(right);
  for (let index = 0; index < a.length; index += 1) {
    if (a[index] < b[index]) return -1;
    if (a[index] > b[index]) return 1;
  }
  return 0;
};

const buildRoundOf32Pairings = (groupTables) => {
  const groups = "ABCDEFGHIJKL".split("");
  const winners = groups.map((group) => groupTables.get(group)[0]);
  const runners = groups.map((group) => groupTables.get(group)[1]);
  const bestThirds = groups
    .map((group) => groupTables.get(group)[2])
    .sort(compareQualification)
    .slice(0, 8);
  const rankedRunners = [...runners].sort(compareQualification);
  const seeded = [...winners, ...rankedRunners.slice(0, 4)];
  const unseeded = [...rankedRunners.slice(4), ...bestThirds];
  return seeded.map((seed) => {
    const opponentIndex = Math.max(
      0,
      unseeded.findIndex((opponent) => opponent.team.group !== seed.team.group),
    );
    const [opponent] = unseeded.splice(opponentIndex, 1);
    return [seed.team, opponent.team];
  });
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
          home_slot_key: null,
          away_slot_key: null,
          status: "scheduled",
          home_score: null,
          away_score: null,
          prediction: predictionByMatchId.get(id) ?? null,
        });
        number += 1;
      });
    });
  }

  let previousNumbers = [];
  let semifinalNumbers = [];
  KNOCKOUT_ROUNDS.forEach(([stage, count, start]) => {
    const currentNumbers = Array.from({ length: count }, (_, index) => number + index);
    currentNumbers.forEach((matchNumber, index) => {
      let homeSlot;
      let awaySlot;
      if (stage === "round_of_32") {
        homeSlot = `R32-H${index + 1}`;
        awaySlot = `R32-A${index + 1}`;
      } else if (stage === "third_place") {
        homeSlot = `Loser M${semifinalNumbers[0]}`;
        awaySlot = `Loser M${semifinalNumbers[1]}`;
      } else if (stage === "final") {
        homeSlot = `Winner M${semifinalNumbers[0]}`;
        awaySlot = `Winner M${semifinalNumbers[1]}`;
      } else {
        homeSlot = `Winner M${previousNumbers[index * 2]}`;
        awaySlot = `Winner M${previousNumbers[index * 2 + 1]}`;
      }
      const kickoff = new Date(start + Math.floor(index / 3) * 24 * 60 * 60 * 1000 + (index % 3) * 3 * 60 * 60 * 1000);
      fixtures.push({
        id: `WC26-${String(matchNumber).padStart(3, "0")}`,
        number: matchNumber,
        stage,
        kickoff: kickoff.toISOString(),
        venue_id: VENUE_IDS[(matchNumber - 1) % VENUE_IDS.length],
        group: null,
        home_team: null,
        away_team: null,
        home_slot: friendlySlot(homeSlot),
        away_slot: friendlySlot(awaySlot),
        home_slot_key: homeSlot,
        away_slot_key: awaySlot,
        status: "scheduled",
        home_score: null,
        away_score: null,
        prediction: predictionByMatchId.get(`WC26-${String(matchNumber).padStart(3, "0")}`) ?? null,
      });
    });
    if (stage === "semifinal") semifinalNumbers = currentNumbers;
    if (stage !== "third_place") previousNumbers = currentNumbers;
    number += count;
  });

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
    const canonicalId = row.canonical_match_id ?? row.match_id;
    const matchNumber = Number(
      row.match_number ?? row.number ?? String(canonicalId ?? row.id).match(/\d+$/)?.[0] ?? index + 1,
    );
    const id = String(canonicalId ?? row.id);
    const databasePrediction = predictionsByMatchId.get(id);
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
      stage: stageFromValue(row.stage ?? row.tournament_stage ?? "group"),
      kickoff: new Date(row.kickoff ?? row.match_date).toISOString(),
      venue_id: row.venue_id ?? "TBD",
      group: row.group_code ?? row.group ?? homeTeam?.group ?? null,
      home_team: homeTeam,
      away_team: awayTeam,
      home_slot: homeTeam ? null : friendlySlot(row.home_slot ?? row.home_team ?? null),
      away_slot: awayTeam ? null : friendlySlot(row.away_slot ?? row.away_team ?? null),
      home_slot_key: row.home_slot ?? row.home_team ?? null,
      away_slot_key: row.away_slot ?? row.away_team ?? null,
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
  if (
    canonical.number === databaseMatch.number &&
    canonical.stage === databaseMatch.stage
  ) return true;
  if (
    canonical.stage !== "group" &&
    databaseMatch.stage === canonical.stage &&
    hoursBetween(databaseMatch.kickoff, canonical.kickoff) <= 36
  ) return true;
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
      home_slot_key: databaseMatch.home_team ? null : databaseMatch.home_slot_key ?? canonical.home_slot_key,
      away_slot_key: databaseMatch.away_team ? null : databaseMatch.away_slot_key ?? canonical.away_slot_key,
      status: databaseMatch.status ?? canonical.status,
      home_score: databaseMatch.home_score ?? canonical.home_score ?? null,
      away_score: databaseMatch.away_score ?? canonical.away_score ?? null,
      prediction,
    };
  });
  return resolveKnockoutParticipants(merged);
};

export const resolveKnockoutParticipants = (matches) => {
  const byNumber = new Map(matches.map((match) => [match.number, match]));
  const groupTables = new Map();
  const groupComplete = new Set();
  "ABCDEFGHIJKL".split("").forEach((group) => {
    const groupMatches = matches.filter((match) => match.stage === "group" && match.group === group);
    const teams = [...new Map(
      groupMatches
        .flatMap((match) => [match.home_team, match.away_team])
        .filter(Boolean)
        .map((team) => [team.id, team]),
    ).values()];
    if (teams.length === 4 && groupMatches.length === 6) {
      groupTables.set(group, rankGroup(teams, groupMatches));
      if (groupMatches.every(isCompletedMatch)) groupComplete.add(group);
    }
  });
  const allGroupsComplete = groupComplete.size === 12;
  const r32Pairings = allGroupsComplete ? buildRoundOf32Pairings(groupTables) : [];

  const resolveSlot = (slot, matchNumber, side) => {
    const raw = String(slot ?? "");
    const r32 = raw.match(/^R32-([HA])(\d+)$/i);
    if (r32) {
      const index = Number(r32[2]) - 1;
      if (allGroupsComplete) return r32Pairings[index]?.[side === "home" ? 0 : 1] ?? null;
      if (side === "home") {
        const group = "ABCDEFGHIJKL"[index];
        if (groupComplete.has(group)) return groupTables.get(group)?.[0]?.team ?? null;
      }
      return null;
    }
    const groupSlot = raw.match(/^(Winner|Runner-up|Third(?:-place)?) Group ([A-L])$/i);
    if (groupSlot) {
      const group = groupSlot[2].toUpperCase();
      if (!groupComplete.has(group)) return null;
      const position = groupSlot[1].toLowerCase().startsWith("winner")
        ? 0
        : groupSlot[1].toLowerCase().startsWith("runner")
          ? 1
          : 2;
      return groupTables.get(group)?.[position]?.team ?? null;
    }
    const parentSlot = raw.match(/^(Winner|Loser) M(?:atch )?(\d+)$/i);
    if (parentSlot) {
      const parent = byNumber.get(Number(parentSlot[2]));
      if (!parent) return null;
      return parentSlot[1].toLowerCase() === "winner"
        ? winnerTeam(parent)
        : loserTeam(parent);
    }
    return null;
  };

  return matches.map((match) => {
    if (match.stage === "group") return match;
    const home = match.home_team ?? resolveSlot(match.home_slot_key ?? match.home_slot, match.number, "home");
    const away = match.away_team ?? resolveSlot(match.away_slot_key ?? match.away_slot, match.number, "away");
    const resolved = {
      ...match,
      home_team: home,
      away_team: away,
      home_slot: home ? null : friendlySlot(match.home_slot),
      away_slot: away ? null : friendlySlot(match.away_slot),
    };
    byNumber.set(resolved.number, resolved);
    return resolved;
  });
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
