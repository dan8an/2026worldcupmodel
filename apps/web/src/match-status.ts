import type { Match } from "./types";

const COMPLETED_STATUSES = new Set([
  "completed",
  "finished",
  "ft",
  "aet",
  "pen",
]);

export function isMatchCompleted(match: Match): boolean {
  const status = match.status?.trim().toLowerCase();
  return (
    COMPLETED_STATUSES.has(status) ||
    (match.home_score != null && match.away_score != null)
  );
}

export function chronologicalMatches(matches: Match[]): Match[] {
  return [...matches].sort((left, right) => {
    const kickoffOrder = left.kickoff.localeCompare(right.kickoff);
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
  return new Date(match.kickoff).getTime() <= now.getTime();
}

export function upcomingMatches(
  matches: Match[],
  now: Date = new Date(),
): Match[] {
  return matches
    .filter((match) => !isMatchCompleted(match) && !hasMatchKickedOff(match, now))
    .sort((left, right) => {
      const kickoffOrder = left.kickoff.localeCompare(right.kickoff);
      return kickoffOrder || left.number - right.number;
    });
}

export function completedMatches(
  matches: Match[],
  now: Date = new Date(),
): Match[] {
  return matches
    .filter((match) => isMatchCompleted(match) || hasMatchKickedOff(match, now))
    .sort((left, right) => {
      const kickoffOrder = right.kickoff.localeCompare(left.kickoff);
      return kickoffOrder || right.number - left.number;
    });
}
