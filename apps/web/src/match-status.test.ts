import { describe, expect, it } from "vitest";
import {
  hasFinalScore,
  isCompletedOrPast,
  isMatchCompleted,
  isUpcoming,
  matchSchedule,
  parseKickoffInstant,
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
  it("keeps a future match later today out of Results", () => {
    const now = new Date("2026-06-12T18:00:00+00:00");
    const schedule = matchSchedule([
      match({ id: "future-later-today", kickoff: "2026-06-12T20:00:00+00:00" }),
    ], now);

    expect(schedule.upcoming.map((item) => item.id)).toEqual(["future-later-today"]);
    expect(schedule.results).toEqual([]);
  });

  it("shows a completed match with a final score in Results", () => {
    const now = new Date("2026-06-12T18:00:00+00:00");
    const schedule = matchSchedule([
      match({
        id: "completed-scored",
        kickoff: "2026-06-12T20:00:00+00:00",
        status: "completed",
        home_score: 2,
        away_score: 1,
      }),
    ], now);

    expect(schedule.upcoming).toEqual([]);
    expect(schedule.results.map((item) => item.id)).toEqual(["completed-scored"]);
  });

  it("shows a just-kicked-off match with no score in Results", () => {
    const now = new Date("2026-06-12T20:00:01+00:00");
    const schedule = matchSchedule([
      match({
        id: "awaiting-final-score",
        kickoff: "2026-06-12T20:00:00+00:00",
        status: "scheduled",
        home_score: null,
        away_score: null,
      }),
    ], now);

    expect(hasFinalScore(schedule.results[0])).toBe(false);
    expect(schedule.results.map((item) => item.id)).toEqual(["awaiting-final-score"]);
  });

  it("handles UTC timestamps near midnight without local calendar-date classification", () => {
    const now = new Date("2026-06-11T23:30:00+00:00");

    expect(isUpcoming(
      match({ id: "future-midnight", kickoff: "2026-06-12T00:30:00+00:00" }),
      now,
    )).toBe(true);
    expect(isCompletedOrPast(
      match({ id: "past-midnight", kickoff: "2026-06-11T23:00:00+00:00" }),
      now,
    )).toBe(true);
  });

  it("does not treat date-only kickoff fields as midnight instants", () => {
    const now = new Date("2026-06-12T18:00:00+00:00");

    expect(isUpcoming(match({ kickoff: "2026-06-12" }), now)).toBe(true);
    expect(isCompletedOrPast(match({ kickoff: "2026-06-12" }), now)).toBe(false);
  });

  it("normalizes timezone-less date-time kickoff fields as UTC instants", () => {
    expect(parseKickoffInstant("2026-06-12T20:00:00")).toBe(
      Date.parse("2026-06-12T20:00:00Z"),
    );
  });

  it("keeps completed matches off the dashboard", () => {
    const now = new Date("2026-06-11T16:00:00+00:00");
    const schedule = matchSchedule([
      match({ id: "finished", status: "finished" }),
      match({ id: "scored", home_score: 2, away_score: 1 }),
      match({ id: "upcoming", kickoff: "2026-06-12T17:00:00+00:00" }),
    ], now);

    expect(schedule.upcoming.map((item) => item.id)).toEqual(["upcoming"]);
  });

  it("keeps already-kicked-off matches off the dashboard until scores arrive", () => {
    const now = new Date("2026-06-12T18:00:00+00:00");
    const schedule = matchSchedule([
      match({ id: "past-unscored", kickoff: "2026-06-12T17:00:00+00:00" }),
      match({ id: "future", kickoff: "2026-06-12T20:00:00+00:00" }),
    ], now);

    expect(schedule.upcoming.map((item) => item.id)).toEqual(["future"]);
  });

  it("shows status-complete, score-complete, and already-kicked-off matches in Results", () => {
    const now = new Date("2026-06-12T18:00:00+00:00");
    const schedule = matchSchedule([
      match({ id: "completed", status: "completed" }),
      match({ id: "scored", status: "scheduled", home_score: 0, away_score: 0 }),
      match({ id: "past-unscored", kickoff: "2026-06-12T17:00:00+00:00" }),
      match({ id: "upcoming", kickoff: "2026-06-12T20:00:00+00:00" }),
    ], now);

    expect(schedule.results.map((item) => item.id)).toEqual([
      "past-unscored",
      "completed",
      "scored",
    ]);
    expect(isMatchCompleted(match({ status: "FT" }))).toBe(true);
  });

  it("maps display match numbers by full chronological order", () => {
    const schedule = matchSchedule([
      match({ id: "group-order-1", number: 1, kickoff: "2026-06-11T17:00:00+00:00" }),
      match({ id: "group-order-9", number: 9, kickoff: "2026-06-12T17:00:00+00:00" }),
    ]);

    expect(schedule.numberById.get("group-order-1")).toBe(1);
    expect(schedule.numberById.get("group-order-9")).toBe(2);
  });

  it("keeps original chronological numbers after filtering completed and past matches", () => {
    const now = new Date("2026-06-12T18:00:00+00:00");
    const schedule = matchSchedule([
      match({ id: "match-1", number: 1, status: "completed", kickoff: "2026-06-11T17:00:00+00:00" }),
      match({ id: "match-2", number: 2, status: "scheduled", kickoff: "2026-06-12T17:00:00+00:00" }),
      match({ id: "match-3", number: 9, status: "scheduled", kickoff: "2026-06-12T20:00:00+00:00" }),
    ], now);

    expect(schedule.upcoming.map((item) => item.id)).toEqual(["match-3"]);
    expect(schedule.numberById.get("match-3")).toBe(3);
  });

  it("recognizes live and likely provider completion field variants", () => {
    const now = new Date("2026-06-12T18:00:00+00:00");
    const completedFlagMatch = {
      ...match({ id: "completed-flag" }),
      completed: true,
    } as Match;
    const camelScoreMatch = {
      ...match({ id: "camel-score" }),
      homeScore: 2,
      awayScore: 1,
    } as Match;
    const matchDateMatch = {
      ...match({ id: "match-date" }),
      kickoff: "",
      match_date: "2026-06-12T17:00:00+00:00",
    } as Match;

    expect(isMatchCompleted(completedFlagMatch)).toBe(true);
    expect(hasFinalScore(camelScoreMatch)).toBe(true);
    expect(isCompletedOrPast(matchDateMatch, now)).toBe(true);
    expect(isUpcoming(match({ kickoff: "2026-06-12T20:00:00+00:00" }), now)).toBe(true);
  });
});
