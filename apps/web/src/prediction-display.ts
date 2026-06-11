import type { Match, Prediction } from "./types";

export type OutcomeProbabilities = {
  home: number;
  draw: number;
  away: number;
};

export type ConfidenceLevel = "Very High" | "High" | "Medium" | "Low";
export type FactorImportance = "high" | "medium" | "low";

export type DisplayFactor = {
  factor: string;
  team: string;
  impact: string;
  value: number;
  direction: "positive" | "negative" | "neutral";
  importance: FactorImportance;
};

export const finalProbabilities = (
  prediction: Prediction,
): OutcomeProbabilities => ({
  home: prediction.final_home_probability ?? prediction.probabilities.home_win,
  draw: prediction.final_draw_probability ?? prediction.probabilities.draw,
  away: prediction.final_away_probability ?? prediction.probabilities.away_win,
});

export const eloBaseProbabilities = (
  prediction: Prediction,
): OutcomeProbabilities | null => {
  const { elo_base_home_probability, elo_base_draw_probability, elo_base_away_probability } =
    prediction;
  if (
    elo_base_home_probability == null ||
    elo_base_draw_probability == null ||
    elo_base_away_probability == null
  ) {
    return null;
  }
  return {
    home: elo_base_home_probability,
    draw: elo_base_draw_probability,
    away: elo_base_away_probability,
  };
};

export const confidenceLevel = (score: number | null | undefined): ConfidenceLevel => {
  if (score == null) return "Low";
  if (score >= 85) return "Very High";
  if (score >= 71) return "High";
  if (score >= 55) return "Medium";
  return "Low";
};

export const confidenceExplanation = (
  score: number | null | undefined,
): string => {
  const level = confidenceLevel(score);
  if (level === "Very High") {
    return "The leading outcome is clearly separated and the model inputs are highly consistent.";
  }
  if (level === "High") {
    return "The model shows a meaningful edge with generally stable supporting inputs.";
  }
  if (level === "Medium") {
    return "The model identifies an edge, but competing outcomes remain plausible.";
  }
  return "The outcomes are closely matched or the available evidence is limited.";
};

const impactValue = (impact: string) => {
  const parsed = Number.parseFloat(impact.replace("%", ""));
  return Number.isFinite(parsed) ? parsed : 0;
};

const normalizedFactorName = (factor: string) => {
  if (factor === "Shot volume") return "Shot-volume edge";
  return factor;
};

const factorImportance = (factor: string): FactorImportance => {
  const label = factor.toLowerCase();
  if (
    label.includes("elo") ||
    label.includes("attack") ||
    label.includes("shot-volume")
  ) {
    return "high";
  }
  if (label.includes("defense") || label.includes("draw calibration")) {
    return "medium";
  }
  if (
    label.includes("home") ||
    label.includes("venue") ||
    label.includes("confederation")
  ) {
    return "medium";
  }
  return "low";
};

const shouldHideFactor = (factor: string, value: number) => {
  const label = factor.toLowerCase();
  return (
    label.includes("travel") &&
    Math.abs(value) < 0.3
  );
};

const importanceRank: Record<FactorImportance, number> = {
  high: 0,
  medium: 1,
  low: 2,
};

const modelImportanceRank = (factor: string) => {
  const label = factor.toLowerCase();
  if (label.includes("elo")) return 0;
  if (label.includes("shot-volume")) return 1;
  if (label.includes("attack")) return 2;
  if (label.includes("defense")) return 3;
  return 4;
};

export const displayFactors = (prediction: Prediction): DisplayFactor[] =>
  (prediction.top_factors ?? []).map((factor): DisplayFactor => {
    const name = normalizedFactorName(factor.factor);
    const value = impactValue(factor.impact);
    return {
      ...factor,
      factor: name,
      value,
      direction: value > 0 ? "positive" : value < 0 ? "negative" : "neutral",
      importance: factorImportance(name),
    };
  }).filter((factor) => !shouldHideFactor(factor.factor, factor.value))
    .sort(
      (left, right) =>
        modelImportanceRank(left.factor) - modelImportanceRank(right.factor) ||
        importanceRank[left.importance] - importanceRank[right.importance] ||
        Math.abs(right.value) - Math.abs(left.value),
    );

export const primaryFactors = (prediction: Prediction): DisplayFactor[] =>
  displayFactors(prediction).slice(0, 3);

export const additionalFactors = (prediction: Prediction): DisplayFactor[] =>
  displayFactors(prediction).slice(3);

const factorPhrase = (factor: DisplayFactor) => {
  const label = factor.factor.toLowerCase();
  if (label.includes("attack")) {
    return `the attack rating favors ${factor.team}`;
  }
  if (label.includes("defense")) {
    return `the defense rating favors ${factor.team}`;
  }
  if (label.includes("draw")) {
    return `${factor.direction === "negative" ? "draw calibration reduces" : "draw calibration increases"} the likelihood of a stalemate`;
  }
  if (label.includes("shot-volume")) {
    return `sustained shot volume favors ${factor.team}`;
  }
  if (label.includes("elo")) {
    return `the Elo baseline favors ${factor.team}`;
  }
  return `${factor.factor.toLowerCase()} favors ${factor.team}`;
};

export const predictionSummary = (match: Match): string => {
  const prediction = match.prediction;
  if (!prediction) return "A model explanation is not available for this match.";
  const final = finalProbabilities(prediction);
  const base = eloBaseProbabilities(prediction);
  const factors = primaryFactors(prediction);
  const homeName = match.home_team?.name ?? "The home team";
  const awayName = match.away_team?.name ?? "The away team";
  const finalFavorite = final.home >= final.away ? homeName : awayName;
  const favoriteFinal = Math.max(final.home, final.away);
  const favoriteBase = base
    ? finalFavorite === homeName
      ? base.home
      : base.away
    : null;
  const movement =
    favoriteBase == null
      ? "leads the model forecast"
      : favoriteFinal >= favoriteBase
        ? "rises above the Elo baseline"
        : "finishes below the Elo baseline";
  const contextFactors = factors.filter(
    (factor) => !factor.factor.toLowerCase().includes("elo"),
  );
  if (!contextFactors.length) {
    return `${finalFavorite}'s win probability ${movement}, with no additional context factor large enough to highlight.`;
  }
  const phrases = contextFactors.slice(0, 2).map(factorPhrase);
  const explanation =
    phrases.length === 1 ? phrases[0] : `${phrases[0]}, while ${phrases[1]}`;
  return `${finalFavorite}'s win probability ${movement} because ${explanation}.`;
};
