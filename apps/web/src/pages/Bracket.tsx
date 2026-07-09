import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { api } from "../api";
import { ErrorState, Loading, TeamLabel } from "../components";
import { hasFinalScore, matchScores } from "../match-status";
import type { Match } from "../types";

const rounds = [
  ["round_of_32", "Round of 32"],
  ["round_of_16", "Round of 16"],
  ["quarterfinal", "Quarterfinals"],
  ["semifinal", "Semifinals"],
  ["final", "Final"],
  ["third_place", "Third-place"],
] as const;

export function Bracket() {
  const query = useQuery({ queryKey: ["matches"], queryFn: api.matches });
  const matchesByStage = useMemo(() => {
    const grouped = new Map<string, Match[]>();
    for (const [stage] of rounds) {
      grouped.set(stage, []);
    }
    for (const match of query.data ?? []) {
      if (!grouped.has(match.stage)) continue;
      grouped.get(match.stage)?.push(match);
    }
    for (const [stage, matches] of grouped) {
      grouped.set(
        stage,
        [...matches].sort((left, right) =>
          (left.number || 999) - (right.number || 999) ||
          left.kickoff.localeCompare(right.kickoff)
        ),
      );
    }
    return grouped;
  }, [query.data]);
  if (query.isLoading) return <Loading label="Loading bracket" />;
  if (query.isError) return <ErrorState />;
  return (
    <section>
      <div className="page-heading">
        <span className="eyebrow">Knockout stage</span>
        <h1>Tournament bracket</h1>
        <p>Qualified teams replace bracket slots as results become final.</p>
      </div>
      <div className="bracket">
        {rounds.map(([stage, label]) => (
          <div className="bracket-round" key={stage}>
            <h2>{label}</h2>
            {(matchesByStage.get(stage) ?? []).map((match) => {
              const score = matchScores(match);
              const showScore = hasFinalScore(match);
              const homeWon = showScore && score.home != null && score.away != null && score.home > score.away;
              const awayWon = showScore && score.home != null && score.away != null && score.away > score.home;
              return (
                <article className="match-card bracket-match" key={match.id}>
                  <small>{match.number > 0 ? `Match ${match.number}` : label}</small>
                  <div className={`bracket-team${homeWon ? " winner" : ""}`}>
                    <strong><TeamLabel team={match.home_team} placeholder={match.home_slot} /></strong>
                    {showScore && <span>{score.home}</span>}
                  </div>
                  <div className={`bracket-team${awayWon ? " winner" : ""}`}>
                    <strong><TeamLabel team={match.away_team} placeholder={match.away_slot} /></strong>
                    {showScore && <span>{score.away}</span>}
                  </div>
                </article>
              );
            })}
          </div>
        ))}
      </div>
    </section>
  );
}
