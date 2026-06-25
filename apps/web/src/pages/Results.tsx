import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api";
import { ErrorState, Loading, ProbabilityBar } from "../components";
import { matchSchedule, matchScores, type MatchClassification } from "../match-status";
import type { Match } from "../types";

function ResultCard({
  match,
  classification,
  displayNumber,
}: {
  match: Match;
  classification?: MatchClassification;
  displayNumber?: number;
}) {
  const hasScore = classification?.hasRealFinalScore ?? false;
  const score = matchScores(match);
  return (
    <Link className="match-card result-card" to={`/match/${match.id}`}>
      <div className="eyebrow">
        Match {displayNumber ?? match.number} · {new Date(match.kickoff).toLocaleDateString()} · Group {match.group}
      </div>
      <div className="result-score">
        <span>{match.home_team?.name ?? match.home_slot}</span>
        <strong>{score.home ?? "–"} - {score.away ?? "–"}</strong>
        <span>{match.away_team?.name ?? match.away_slot}</span>
      </div>
      {!hasScore && (
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
  const schedule = matchSchedule(matchData);
  return (
    <section>
      <div className="page-heading">
        <span className="eyebrow">Completed matches</span>
        <h1>Results</h1>
        <p>Final scores alongside the model prediction available before kickoff.</p>
      </div>
      {schedule.results.length > 0 ? (
        <div className="card-grid">
          {schedule.results.map((match) => (
            <ResultCard
              key={match.id}
              match={match}
              classification={schedule.classificationById.get(match.id)}
              displayNumber={schedule.numberById.get(match.id)}
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
