import type { Match } from "./types";

type MatchPayload = Match & Record<string, unknown>;

const COMPLETED_STATUSES = new Set([
  "completed",
  "finished",
  "ft",
  "aet",
  "pen",
]);

const IN_PROGRESS_STATUSES = new Set([
  "live",
  "in progress",
  "in_progress",
  "in-progress",
  "playing",
  "1h",
  "ht",
  "2h",
  "et",
  "bt",
  "p",
  "int",
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

function matchStatus(match: Match): string | null {
  return stringField(match as MatchPayload, ["status", "state"])?.trim().toLowerCase() ?? null;
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

const DATE_ONLY_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const ISO_DATE_TIME_PATTERN =
  /^\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$/i;
const TIMEZONE_PATTERN = /(Z|[+-]\d{2}:?\d{2})$/i;

export function parseKickoffInstant(value: string | null): number {
  const kickoff = value?.trim();
  if (!kickoff || DATE_ONLY_PATTERN.test(kickoff) || !ISO_DATE_TIME_PATTERN.test(kickoff)) {
    return Number.POSITIVE_INFINITY;
  }

  const normalized = TIMEZONE_PATTERN.test(kickoff) ? kickoff : `${kickoff}Z`;
  const time = new Date(normalized).getTime();
  return Number.isNaN(time) ? Number.POSITIVE_INFINITY : time;
}

export function matchKickoffTime(match: Match): number {
  const kickoff = stringField(match as MatchPayload, [
    "kickoff",
    "match_date",
    "matchDate",
    "date",
  ]);
  return parseKickoffInstant(kickoff);
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
  const score = matchScores(match);
  return score.home != null && score.away != null;
}

export function isMatchCompleted(match: Match): boolean {
  const status = matchStatus(match);
  return (
    booleanField(match as MatchPayload, ["completed", "is_completed", "isCompleted"]) ||
    (status != null && COMPLETED_STATUSES.has(status)) ||
    hasFinalScore(match)
  );
}

export function isFinalResult(match: Match): boolean {
  return isMatchCompleted(match);
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
  const kickoffTime = matchKickoffTime(match);
  return Number.isFinite(kickoffTime) && kickoffTime <= now.getTime();
}

export function isMatchInProgress(
  match: Match,
  now: Date = new Date(),
): boolean {
  if (isFinalResult(match)) return false;
  const status = matchStatus(match);
  return (
    (status != null && IN_PROGRESS_STATUSES.has(status)) ||
    hasMatchKickedOff(match, now)
  );
}

export function isCompletedOrPast(
  match: Match,
  _now: Date = new Date(),
): boolean {
  void _now;
  return isFinalResult(match);
}

export function isUpcoming(
  match: Match,
  now: Date = new Date(),
): boolean {
  return !isFinalResult(match) && !isMatchInProgress(match, now);
}

export function belongsOnActiveMatchPages(
  match: Match,
  _now: Date = new Date(),
): boolean {
  void _now;
  return !isFinalResult(match);
}

export function upcomingMatches(
  matches: Match[],
  now: Date = new Date(),
): Match[] {
  return matches
    .filter((match) => belongsOnActiveMatchPages(match, now))
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
    upcoming: ordered.filter((match) => belongsOnActiveMatchPages(match, now)),
    results: ordered
      .filter((match) => isFinalResult(match))
      .sort((left, right) => matchKickoffTime(right) - matchKickoffTime(left) || right.number - left.number),
  };
}
