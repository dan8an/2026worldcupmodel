import { describe, expect, it } from "vitest";
import {
  confidenceLevel,
  displayFactors,
  finalProbabilities,
  predictionSummary,
} from "./prediction-display";
import type { Match, Prediction } from "./types";

const prediction = {
  probabilities: { home_win: 0.52, draw: 0.27, away_win: 0.21 },
  final_home_probability: 0.56,
  final_draw_probability: 0.28,
  final_away_probability: 0.16,
  elo_base_home_probability: 0.52,
  elo_base_draw_probability: 0.27,
  elo_base_away_probability: 0.21,
  confidence_score: 0.72,
  top_factors: [
    { factor: "Elo advantage", team: "Brazil", impact: "+4.2%" },
    { factor: "Attack/defense edge", team: "Brazil", impact: "+1.6%" },
    { factor: "Draw calibration", team: "Draw", impact: "+0.8%" },
    { factor: "Rest context", team: "Morocco", impact: "-0.2%" },
  ],
} as Prediction;

const match = {
  home_team: { name: "Brazil" },
  away_team: { name: "Morocco" },
  prediction,
} as Match;

describe("prediction display helpers", () => {
  it("prefers final probabilities and maps confidence thresholds", () => {
    expect(finalProbabilities(prediction)).toEqual({
      home: 0.56,
      draw: 0.28,
      away: 0.16,
    });
    expect(confidenceLevel(0.7)).toBe("High");
    expect(confidenceLevel(0.5)).toBe("Medium");
    expect(confidenceLevel(0.49)).toBe("Low");
  });

  it("limits factors, preserves impact direction, and builds summary copy", () => {
    expect(displayFactors(prediction)).toHaveLength(3);
    expect(displayFactors(prediction)[0].direction).toBe("positive");
    expect(predictionSummary(match)).toBe(
      "Brazil's win probability rises above the Elo baseline because stronger attack and defense ratings favor Brazil, while draw calibration increases the likelihood of a stalemate.",
    );
  });
});
