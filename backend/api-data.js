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
    const id = String(row.id);
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
      number: Number(
        row.match_number ?? row.number ?? id.match(/\d+$/)?.[0] ?? index + 1,
      ),
      stage: row.stage ?? row.tournament_stage ?? "group",
      kickoff: new Date(row.kickoff ?? row.match_date).toISOString(),
      venue_id: row.venue_id ?? "TBD",
      group: row.group_code ?? row.group ?? homeTeam?.group ?? null,
      home_team: homeTeam,
      away_team: awayTeam,
      home_slot: row.home_slot ?? null,
      away_slot: row.away_slot ?? null,
      prediction,
    };
  }).sort((a, b) =>
    a.kickoff.localeCompare(b.kickoff) || a.number - b.number
  );
};

const sameUtcDate = (left, right) =>
  left.slice(0, 10) === right.slice(0, 10);

const matchesCanonicalFixture = (canonical, databaseMatch) => {
  if (canonical.id === databaseMatch.id) return true;
  if (!canonical.home_team || !canonical.away_team) return false;
  return databaseMatch.home_team?.id === canonical.home_team.id &&
    databaseMatch.away_team?.id === canonical.away_team.id &&
    sameUtcDate(databaseMatch.kickoff, canonical.kickoff);
};

export const mergeDatabaseMatches = (canonicalMatches, databaseMatches) =>
  canonicalMatches.map((canonical) => {
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
      prediction,
    };
  });

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
  return {
    ...generatedSnapshot.simulation,
    model_version: generatedSnapshot.model_version ?? "placeholder",
    generated_at: generatedSnapshot.generated_at ?? new Date(0).toISOString(),
    data_cutoff: generatedSnapshot.data_cutoff ?? new Date(0).toISOString(),
  };
};

export const normalizeDatabaseSimulation = (run, probabilities, teams) => ({
  iterations: Number(run.num_simulations ?? run.iterations),
  seed: Number(run.random_seed ?? run.seed ?? 2026),
  model_version: run.model_version ?? run.semantic_version ?? "supabase",
  generated_at: run.generated_at ?? run.created_at,
  data_cutoff: run.data_cutoff ?? run.generated_at ?? run.created_at,
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
