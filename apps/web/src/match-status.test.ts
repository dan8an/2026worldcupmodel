import { describe, expect, it } from "vitest";
import {
  completedMatches,
  isMatchCompleted,
  upcomingMatches,
} from "./match-status";
import type { Match } from "./types";

const match = (overrides: Partial<Match>): Match => ({
  id: "WC26-001",
  number: 1,
  stage: "group",
  kickoff: "2026-06-11T17:00:00+00:00",
  venue_id: "MEX",
  group: "A",
  home_team: null,
  away_team: null,
  home_slot: null,
  away_slot: null,
  status: "scheduled",
  home_score: null,
  away_score: null,
  prediction: null,
  ...overrides,
});

describe("match completion filtering", () => {
  it("keeps completed matches off the dashboard", () => {
    const upcoming = upcomingMatches([
      match({ id: "finished", status: "finished" }),
      match({ id: "scored", home_score: 2, away_score: 1 }),
      match({ id: "upcoming", kickoff: "2026-06-12T17:00:00+00:00" }),
    ]);

    expect(upcoming.map((item) => item.id)).toEqual(["upcoming"]);
  });

  it("shows status-complete and score-complete matches in Results", () => {
    const results = completedMatches([
      match({ id: "completed", status: "completed" }),
      match({ id: "scored", status: "scheduled", home_score: 0, away_score: 0 }),
      match({ id: "upcoming" }),
    ]);

    expect(results.map((item) => item.id)).toEqual(["completed", "scored"]);
    expect(isMatchCompleted(match({ status: "FT" }))).toBe(true);
  });
});
