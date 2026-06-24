import { Link } from "react-router-dom";
import {
  confidenceExplanation,
  confidenceLevel,
  displayFactors,
  finalProbabilities,
} from "./prediction-display";
import type { Match } from "./types";

export const percent = (value: number) => `${Math.round(value * 100)}%`;
export const precisePercent = (value: number) => `${(value * 100).toFixed(1)}%`;

export function Loading({ label = "Loading forecast" }: { label?: string }) {
  return <div className="state-card">{label}...</div>;
}

export function ErrorState() {
  return (
    <div className="state-card error">
      The forecast API is unavailable. Start FastAPI on port 8000 and retry.
    </div>
  );
}

export function ProbabilityBar({ match }: { match: Match }) {
  const prediction = match.prediction;
  if (!prediction) return <p className="muted">Prediction available after teams qualify.</p>;
  const probabilities = finalProbabilities(prediction);
  return (
    <div className="probability-visual">
      <div className="probability-labels">
        <span><b>{percent(probabilities.home)}</b> Win</span>
        <span><b>{percent(probabilities.draw)}</b> Draw</span>
        <span><b>{percent(probabilities.away)}</b> Loss</span>
      </div>
      <div className="probability-bar" aria-label="Result probabilities">
        <span className="home" style={{ width: `${probabilities.home * 100}%` }} />
        <span className="draw" style={{ width: `${probabilities.draw * 100}%` }} />
        <span className="away" style={{ width: `${probabilities.away * 100}%` }} />
      </div>
    </div>
  );
}

export function MatchCard({
  match,
  displayNumber,
}: {
  match: Match;
  displayNumber?: number;
}) {
  const confidence = match.prediction
    ? confidenceLevel(match.prediction.confidence_score)
    : null;
  const leadFactor = match.prediction ? displayFactors(match.prediction)[0] : null;
  const matchNumber = displayNumber ?? match.number;
  return (
    <Link className="match-card" to={`/match/${match.id}`}>
      <div className="eyebrow">
        Group {match.group} · Match {matchNumber}
      </div>
      <div className="teams-row">
        <strong>
          {match.home_team && <span className="flag small-flag" aria-hidden="true">{match.home_team.flag}</span>}
          {match.home_team?.name ?? match.home_slot}
        </strong>
        <span>vs</span>
        <strong>
          {match.away_team && <span className="flag small-flag" aria-hidden="true">{match.away_team.flag}</span>}
          {match.away_team?.name ?? match.away_slot}
        </strong>
      </div>
      {match.prediction && (
        <>
          <ProbabilityBar match={match} />
          <div className="match-meta">
            <span>
              xG {match.prediction.home_xg.toFixed(2)} - {match.prediction.away_xg.toFixed(2)}
            </span>
            <span
              className={`confidence-pill ${confidence?.toLowerCase().replace(" ", "-")}`}
              title={confidenceExplanation(match.prediction.confidence_score)}
            >
              {confidence} confidence
            </span>
          </div>
          {leadFactor && (
            <div className="card-factor">
              <span>{leadFactor.factor}</span>
              <strong className={leadFactor.direction}>
                {leadFactor.direction === "negative" ? "↓" : "↑"} {leadFactor.impact}
              </strong>
            </div>
          )}
        </>
      )}
    </Link>
  );
}
