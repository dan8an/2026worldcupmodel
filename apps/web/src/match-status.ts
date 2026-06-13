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

export function upcomingMatches(matches: Match[]): Match[] {
  return matches
    .filter((match) => !isMatchCompleted(match))
    .sort((left, right) => left.kickoff.localeCompare(right.kickoff));
}

export function completedMatches(matches: Match[]): Match[] {
  return matches
    .filter(isMatchCompleted)
    .sort((left, right) => right.kickoff.localeCompare(left.kickoff));
}
