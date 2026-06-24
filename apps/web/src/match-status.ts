import type { Match } from "./types";

type MatchPayload = Match & Record<string, unknown>;

const COMPLETED_STATUSES = new Set([
  "completed",
  "finished",
  "ft",
  "aet",
  "pen",
]);

function stringField(match: MatchPayload, keys: string[]): string | null {
  for (const key of keys) {
    const value = match[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return null;
}

function booleanField(match: MatchPayload, keys: string[]): boolean {
  return keys.some((key) => match[key] === true);
}

function presentField(match: MatchPayload, keys: string[]): boolean {
  return keys.some((key) => match[key] != null);
}

function numberField(match: MatchPayload, keys: string[]): number | null {
  for (const key of keys) {
    const value = match[key];
    if (typeof value === "number") return value;
    if (typeof value === "string" && value.trim() !== "" && !Number.isNaN(Number(value))) {
      return Number(value);
    }
  }
  return null;
}

export function matchKickoffTime(match: Match): number {
  const kickoff = stringField(match as MatchPayload, [
    "kickoff",
    "match_date",
    "matchDate",
    "date",
  ]);
  const time = kickoff ? new Date(kickoff).getTime() : Number.NaN;
  return Number.isNaN(time) ? Number.POSITIVE_INFINITY : time;
}

export function matchScores(match: Match): {
  home: number | null;
  away: number | null;
} {
  return {
    home: numberField(match as MatchPayload, [
      "home_score",
      "homeScore",
      "home_goals",
      "homeGoals",
    ]),
    away: numberField(match as MatchPayload, [
      "away_score",
      "awayScore",
      "away_goals",
      "awayGoals",
    ]),
  };
}

export function hasFinalScore(match: Match): boolean {
  const payload = match as MatchPayload;
  const score = matchScores(match);
  return (
    (score.home != null && score.away != null) ||
    (presentField(payload, ["home_score", "homeScore", "home_goals", "homeGoals"]) &&
      presentField(payload, ["away_score", "awayScore", "away_goals", "awayGoals"]))
  );
}

export function isMatchCompleted(match: Match): boolean {
  const status = stringField(match as MatchPayload, ["status", "state"])?.trim().toLowerCase();
  return (
    booleanField(match as MatchPayload, ["completed", "is_completed", "isCompleted"]) ||
    (status != null && COMPLETED_STATUSES.has(status)) ||
    hasFinalScore(match)
  );
}

export function chronologicalMatches(matches: Match[]): Match[] {
  return [...matches].sort((left, right) => {
    const kickoffOrder = matchKickoffTime(left) - matchKickoffTime(right);
    return kickoffOrder || left.number - right.number;
  });
}

export function chronologicalMatchNumbers(matches: Match[]): Map<string, number> {
  return new Map(
    chronologicalMatches(matches).map((match, index) => [match.id, index + 1]),
  );
}

export function hasMatchKickedOff(
  match: Match,
  now: Date = new Date(),
): boolean {
  return matchKickoffTime(match) <= now.getTime();
}

export function isCompletedOrPast(
  match: Match,
  now: Date = new Date(),
): boolean {
  return isMatchCompleted(match) || hasMatchKickedOff(match, now);
}

export function isUpcoming(
  match: Match,
  now: Date = new Date(),
): boolean {
  return !isCompletedOrPast(match, now);
}

export function upcomingMatches(
  matches: Match[],
  now: Date = new Date(),
): Match[] {
  return matches
    .filter((match) => isUpcoming(match, now))
    .sort((left, right) => matchKickoffTime(left) - matchKickoffTime(right) || left.number - right.number);
}

export function completedMatches(
  matches: Match[],
  now: Date = new Date(),
): Match[] {
  return matches
    .filter((match) => isCompletedOrPast(match, now))
    .sort((left, right) => {
      const kickoffOrder = matchKickoffTime(right) - matchKickoffTime(left);
      return kickoffOrder || right.number - left.number;
    });
}

export function matchSchedule(matches: Match[], now: Date = new Date()): {
  numberById: Map<string, number>;
  upcoming: Match[];
  results: Match[];
} {
  const ordered = chronologicalMatches(matches);
  const numberById = new Map(
    ordered.map((match, index) => [match.id, index + 1]),
  );
  return {
    numberById,
    upcoming: ordered.filter((match) => isUpcoming(match, now)),
    results: ordered
      .filter((match) => isCompletedOrPast(match, now))
      .sort((left, right) => matchKickoffTime(right) - matchKickoffTime(left) || right.number - left.number),
  };
}
