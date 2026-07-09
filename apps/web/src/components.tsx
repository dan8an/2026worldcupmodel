import { Link } from "react-router-dom";
import {
  confidenceExplanation,
  confidenceLevel,
  displayFactors,
  finalProbabilities,
} from "./prediction-display";
import { isMatchInProgress } from "./match-status";
import type { Match, Team } from "./types";

export const percent = (value: number) => `${Math.round(value * 100)}%`;
export const precisePercent = (value: number) => `${(value * 100).toFixed(1)}%`;

export function TeamLabel({ team, placeholder }: { team: Team | null; placeholder: string | null }) {
  if (!team) return <>{placeholder ?? "Team to be determined"}</>;
  return (
    <>
      <span className="flag small-flag" aria-hidden="true">{team.flag}</span>
      {team.name} <small className="team-code">{team.id}</small>
    </>
  );
}

export function matchStageLabel(match: Match) {
  if (match.group) return `Group ${match.group}`;
  const labels: Record<string, string> = {
    round_of_32: "Round of 32",
    round_of_16: "Round of 16",
    quarterfinal: "Quarterfinal",
    semifinal: "Semifinal",
    third_place: "Third-place Match",
    final: "Final",
  };
  if (labels[match.stage]) return labels[match.stage];
  return match.stage
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

export function Loading({ label = "Loading forecast" }: { label?: string }) {
  return <div className="state-card">{label}...</div>;
}

export function ErrorState({
  message = "The forecast API is unavailable. Start FastAPI on port 8000 and retry.",
}: {
  message?: string;
}) {
  return (
    <div className="state-card error">
      {message}
    </div>
  );
}

export function ProbabilityBar({ match }: { match: Match }) {
  const prediction = match.prediction;
  if (!prediction) return <p className="muted">Prediction not available yet.</p>;
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
  const inProgress = isMatchInProgress(match);
  return (
    <Link className="match-card" to={`/match/${match.id}`}>
      <div className="eyebrow">
        {matchStageLabel(match)} · Match {matchNumber}
      </div>
      {inProgress && (
        <span className="match-state-badge">In progress · Awaiting score</span>
      )}
      <div className="teams-row">
        <strong>
          <TeamLabel team={match.home_team} placeholder={match.home_slot} />
        </strong>
        <span>vs</span>
        <strong>
          <TeamLabel team={match.away_team} placeholder={match.away_slot} />
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
      {!match.prediction && (
        <p className="muted">Prediction not available yet.</p>
      )}
    </Link>
  );
}
