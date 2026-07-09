import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api";
import { ErrorState, Loading, matchStageLabel, ProbabilityBar, TeamLabel } from "../components";
import { hasFinalScore, matchSchedule, matchScores } from "../match-status";
import type { Match } from "../types";

function ResultCard({
  match,
  displayNumber,
}: {
  match: Match;
  displayNumber?: number;
}) {
  const hasScore = hasFinalScore(match);
  const score = matchScores(match);
  return (
    <Link className="match-card result-card" to={`/match/${match.id}`}>
      <div className="eyebrow">
        Match {displayNumber ?? match.number} · {new Date(match.kickoff).toLocaleDateString()} · {matchStageLabel(match)}
      </div>
      <div className="result-score">
        <span><TeamLabel team={match.home_team} placeholder={match.home_slot} /></span>
        <strong>{score.home ?? "–"} - {score.away ?? "–"}</strong>
        <span><TeamLabel team={match.away_team} placeholder={match.away_slot} /></span>
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
