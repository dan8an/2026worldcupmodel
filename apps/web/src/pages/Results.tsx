import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api";
import { ErrorState, Loading, ProbabilityBar } from "../components";
import { chronologicalMatchNumbers, completedMatches, isMatchCompleted } from "../match-status";
import type { Match } from "../types";

function ResultCard({
  match,
  displayNumber,
}: {
  match: Match;
  displayNumber?: number;
}) {
  const hasScore = match.home_score != null && match.away_score != null;
  return (
    <Link className="match-card result-card" to={`/match/${match.id}`}>
      <div className="eyebrow">
        Match {displayNumber ?? match.number} · {new Date(match.kickoff).toLocaleDateString()} · Group {match.group}
      </div>
      <div className="result-score">
        <span>{match.home_team?.name ?? match.home_slot}</span>
        <strong>{match.home_score ?? "–"} - {match.away_score ?? "–"}</strong>
        <span>{match.away_team?.name ?? match.away_slot}</span>
      </div>
      {!hasScore && !isMatchCompleted(match) && (
        <p className="muted">Awaiting final score.</p>
      )}
      {match.prediction ? (
        <>
          <small className="result-prediction-label">Pre-match prediction</small>
          <ProbabilityBar match={match} />
        </>
      ) : (
        <p className="muted">No pre-match prediction is available.</p>
      )}
    </Link>
  );
}

export function Results() {
  const query = useQuery({ queryKey: ["matches"], queryFn: api.matches });
  if (query.isLoading) return <Loading label="Loading results" />;
  if (query.isError) return <ErrorState />;

  const matchData = query.data ?? [];
  const results = completedMatches(matchData);
  const displayNumbers = chronologicalMatchNumbers(matchData);
  return (
    <section>
      <div className="page-heading">
        <span className="eyebrow">Completed matches</span>
        <h1>Results</h1>
        <p>Final scores alongside the model prediction available before kickoff.</p>
      </div>
      {results.length > 0 ? (
        <div className="card-grid">
          {results.map((match) => (
            <ResultCard
              key={match.id}
              match={match}
              displayNumber={displayNumbers.get(match.id)}
            />
          ))}
        </div>
      ) : (
        <div className="state-card empty-state">
          No completed matches are available yet.
        </div>
      )}
    </section>
  );
}
