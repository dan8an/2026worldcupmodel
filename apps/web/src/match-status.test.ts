import { describe, expect, it } from "vitest";
import {
  hasFinalScore,
  hasMatchKickedOff,
  isCompletedOrPast,
  isMatchInProgress,
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

  it("keeps the live scheduled WC26-024 payload out of Results before kickoff", () => {
    const now = new Date("2026-06-25T19:59:59+00:00");
    const liveScheduledMatch = {
      ...match({
        id: "WC26-024",
        number: 24,
        kickoff: "2026-06-25T20:00:00+00:00",
        status: "scheduled",
        home_score: null,
        away_score: null,
      }),
      home_team: { id: "PAR", name: "Paraguay" },
      away_team: { id: "AUS", name: "Australia" },
      prediction: {
        probabilities: {
          home_win: 0.281031460666646,
          draw: 0.280061343572901,
          away_win: 0.438907195760453,
        },
        final_home_probability: 0.281031460666646,
        final_draw_probability: 0.280061343572901,
        final_away_probability: 0.438907195760453,
        top_scores: [
          { home: 1, away: 1, probability: 0.12940329485773233 },
          { home: 0, away: 1, probability: 0.11785436651834981 },
        ],
      },
    } as Match;
    const schedule = matchSchedule([liveScheduledMatch], now);

    expect(hasFinalScore(liveScheduledMatch)).toBe(false);
    expect(hasMatchKickedOff(liveScheduledMatch, now)).toBe(false);
    expect(isMatchCompleted(liveScheduledMatch)).toBe(false);
    expect(isCompletedOrPast(liveScheduledMatch, now)).toBe(false);
    expect(isUpcoming(liveScheduledMatch, now)).toBe(true);
    expect(schedule.results).toEqual([]);
    expect(schedule.upcoming.map((item) => item.id)).toEqual(["WC26-024"]);
  });

  it("keeps all future scheduled matches from a live-like payload out of Results", () => {
    const now = new Date("2026-06-25T20:06:51+00:00");
    const liveFutureMatches = [
      match({
        id: "WC26-041",
        number: 41,
        kickoff: "2026-06-26T17:00:00+00:00",
        status: "scheduled",
        home_score: null,
        away_score: null,
        home_slot: "New Zealand",
        away_slot: "Belgium",
      }),
      match({
        id: "WC26-047",
        number: 47,
        kickoff: "2026-06-26T17:00:00+00:00",
        status: "scheduled",
        home_score: null,
        away_score: null,
        home_slot: "Uruguay",
        away_slot: "Spain",
      }),
      match({
        id: "WC26-054",
        number: 54,
        kickoff: "2026-06-26T20:00:00+00:00",
        status: "scheduled",
        home_score: null,
        away_score: null,
        home_slot: "Senegal",
        away_slot: "Iraq",
      }),
    ];
    const schedule = matchSchedule(liveFutureMatches, now);

    expect(schedule.results).toEqual([]);
    expect(schedule.upcoming.map((item) => item.id)).toEqual([
      "WC26-041",
      "WC26-047",
      "WC26-054",
    ]);
  });

  it("does not mistake prediction probabilities or projected scores for final scores", () => {
    const now = new Date("2026-06-12T18:00:00+00:00");
    const probabilityOnlyMatch = {
      ...match({
        id: "probability-only",
        kickoff: "2026-06-12T20:00:00+00:00",
        status: "scheduled",
      }),
      home_win_probability: 0.62,
      away_win_probability: 0.18,
      final_home_probability: 0.62,
      final_away_probability: 0.18,
      projected_home_score: 2,
      projected_away_score: 1,
    } as Match;

    expect(hasFinalScore(probabilityOnlyMatch)).toBe(false);
    expect(isCompletedOrPast(probabilityOnlyMatch, now)).toBe(false);
    expect(isUpcoming(probabilityOnlyMatch, now)).toBe(true);
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

  it("keeps a just-kicked-off match with no score on active match pages", () => {
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

    expect(hasFinalScore(schedule.upcoming[0])).toBe(false);
    expect(isMatchInProgress(schedule.upcoming[0], now)).toBe(true);
    expect(schedule.upcoming.map((item) => item.id)).toEqual(["awaiting-final-score"]);
    expect(schedule.results).toEqual([]);
  });

  it("handles UTC timestamps near midnight without local calendar-date classification", () => {
    const now = new Date("2026-06-11T23:30:00+00:00");

    expect(isUpcoming(
      match({ id: "future-midnight", kickoff: "2026-06-12T00:30:00+00:00" }),
      now,
    )).toBe(true);
    expect(hasMatchKickedOff(
      match({ id: "past-midnight", kickoff: "2026-06-11T23:00:00+00:00" }),
      now,
    )).toBe(true);
    expect(isMatchInProgress(
      match({ id: "past-midnight", kickoff: "2026-06-11T23:00:00+00:00" }),
      now,
    )).toBe(true);
  });

  it("does not treat date-only kickoff fields as midnight instants", () => {
    const now = new Date("2026-06-12T18:00:00+00:00");

    expect(isUpcoming(match({ kickoff: "2026-06-12" }), now)).toBe(true);
    expect(isCompletedOrPast(match({ kickoff: "2026-06-12" }), now)).toBe(false);
  });

  it("keeps missing or invalid kickoff matches upcoming unless explicitly completed", () => {
    const now = new Date("2026-06-12T18:00:00+00:00");

    expect(hasMatchKickedOff(match({ kickoff: "" }), now)).toBe(false);
    expect(isCompletedOrPast(match({ kickoff: "" }), now)).toBe(false);
    expect(isUpcoming(match({ kickoff: "" }), now)).toBe(true);
    expect(hasMatchKickedOff(match({ kickoff: "TBD" }), now)).toBe(false);
    expect(isCompletedOrPast(match({ kickoff: "TBD" }), now)).toBe(false);
    expect(isUpcoming(match({ kickoff: "TBD" }), now)).toBe(true);
    expect(isMatchCompleted(match({ kickoff: "", status: "finished" }))).toBe(true);
    expect(isCompletedOrPast(match({ kickoff: "", status: "finished" }), now)).toBe(true);
    expect(isUpcoming(match({ kickoff: "", status: "finished" }), now)).toBe(false);
  });

  it("keeps scheduled provider statuses upcoming before kickoff", () => {
    const now = new Date("2026-06-12T18:00:00+00:00");

    for (const status of ["scheduled", "NS", "TBD"]) {
      const scheduledMatch = match({ kickoff: "2026-06-12T20:00:00+00:00", status });
      expect(isMatchCompleted(scheduledMatch)).toBe(false);
      expect(hasMatchKickedOff(scheduledMatch, now)).toBe(false);
      expect(isCompletedOrPast(scheduledMatch, now)).toBe(false);
      expect(isUpcoming(scheduledMatch, now)).toBe(true);
    }
  });

  it("treats explicit live statuses as active and in progress", () => {
    const now = new Date("2026-06-12T18:00:00+00:00");

    for (const status of ["live", "in_progress", "In Progress", "1H", "HT", "2H"]) {
      const liveMatch = match({
        id: `live-${status}`,
        kickoff: "2026-06-12T20:00:00+00:00",
        status,
      });
      const schedule = matchSchedule([liveMatch], now);

      expect(isMatchInProgress(liveMatch, now)).toBe(true);
      expect(schedule.upcoming).toEqual([liveMatch]);
      expect(schedule.results).toEqual([]);
    }
  });

  it("assigns every match to exactly one side of the active/results partition", () => {
    const now = new Date("2026-06-12T18:00:00+00:00");
    const matches = [
      match({ id: "future", kickoff: "2026-06-12T20:00:00+00:00" }),
      match({ id: "past-unscored", kickoff: "2026-06-12T17:00:00+00:00" }),
      match({ id: "explicit-live", kickoff: "TBD", status: "live" }),
      match({ id: "ambiguous", kickoff: "TBD", status: "unknown" }),
      match({ id: "completed", status: "completed", home_score: 2, away_score: 1 }),
    ];
    const schedule = matchSchedule(matches, now);
    const classifiedIds = [
      ...schedule.upcoming.map((item) => item.id),
      ...schedule.results.map((item) => item.id),
    ];

    expect(new Set(classifiedIds)).toEqual(new Set(matches.map((item) => item.id)));
    expect(classifiedIds).toHaveLength(matches.length);
    expect(schedule.upcoming.map((item) => item.id)).toContain("ambiguous");
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

  it("keeps already-kicked-off matches on active match pages until scores arrive", () => {
    const now = new Date("2026-06-12T18:00:00+00:00");
    const schedule = matchSchedule([
      match({ id: "past-unscored", kickoff: "2026-06-12T17:00:00+00:00" }),
      match({ id: "future", kickoff: "2026-06-12T20:00:00+00:00" }),
    ], now);

    expect(schedule.upcoming.map((item) => item.id)).toEqual(["past-unscored", "future"]);
    expect(isMatchInProgress(schedule.upcoming[0], now)).toBe(true);
  });

  it("shows only final results in Results", () => {
    const now = new Date("2026-06-12T18:00:00+00:00");
    const schedule = matchSchedule([
      match({ id: "completed", status: "completed" }),
      match({ id: "scored", status: "scheduled", home_score: 0, away_score: 0 }),
      match({ id: "past-unscored", kickoff: "2026-06-12T17:00:00+00:00" }),
      match({ id: "upcoming", kickoff: "2026-06-12T20:00:00+00:00" }),
    ], now);

    expect(schedule.results.map((item) => item.id)).toEqual([
      "completed",
      "scored",
    ]);
    expect(schedule.upcoming.map((item) => item.id)).toEqual([
      "past-unscored",
      "upcoming",
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

    expect(schedule.upcoming.map((item) => item.id)).toEqual(["match-2", "match-3"]);
    expect(schedule.numberById.get("match-2")).toBe(2);
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
    expect(isCompletedOrPast(matchDateMatch, now)).toBe(false);
    expect(isMatchInProgress(matchDateMatch, now)).toBe(true);
    expect(isUpcoming(match({ kickoff: "2026-06-12T20:00:00+00:00" }), now)).toBe(true);
  });
});
