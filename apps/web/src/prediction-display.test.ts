import { describe, expect, it } from "vitest";
import {
  additionalFactors,
  confidenceExplanation,
  confidenceLevel,
  displayFactors,
  finalProbabilities,
  primaryFactors,
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
  confidence_score: 72,
  top_factors: [
    { factor: "Elo advantage", team: "Brazil", impact: "+4.2%" },
    { factor: "Attack/defense edge", team: "Brazil", impact: "+1.6%" },
    { factor: "Draw calibration", team: "Draw", impact: "+0.8%" },
    { factor: "Rest context", team: "Morocco", impact: "-0.2%" },
    { factor: "Shot volume", team: "Brazil", impact: "+1.1%" },
    { factor: "Venue context", team: "Brazil", impact: "+1.8%" },
    { factor: "Travel context", team: "Morocco", impact: "-0.5%" },
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
    expect(confidenceLevel(85)).toBe("Very High");
    expect(confidenceLevel(71)).toBe("High");
    expect(confidenceLevel(55)).toBe("Medium");
    expect(confidenceLevel(54.9)).toBe("Low");
    expect(confidenceExplanation(86)).toMatch(/clearly separated/);
    expect(confidenceExplanation(40)).toMatch(/closely matched/);
  });

  it("ranks factors, renames v4 labels, and hides tiny context", () => {
    const factors = displayFactors(prediction);
    expect(factors.map((factor) => factor.factor)).toEqual([
      "Elo advantage",
      "Attack/defense edge",
      "Shot-volume edge",
      "Draw calibration",
      "Venue context",
      "Travel context",
    ]);
    expect(factors[0].direction).toBe("positive");
    expect(primaryFactors(prediction)).toHaveLength(3);
    expect(additionalFactors(prediction)).toHaveLength(3);
    expect(factors.some((factor) => factor.factor === "Rest-day edge")).toBe(false);
  });

  it("builds summary copy from the strongest non-Elo factors", () => {
    expect(predictionSummary(match)).toBe(
      "Brazil's win probability rises above the Elo baseline because stronger attack and defense ratings favor Brazil, while sustained shot volume favors Brazil.",
    );
  });
});
