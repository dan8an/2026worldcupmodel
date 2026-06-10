import { Link } from "react-router-dom";
import type { Match } from "./types";

export const percent = (value: number) => `${Math.round(value * 100)}%`;

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
  const { home_win, draw, away_win } = prediction.probabilities;
  return (
    <div>
      <div className="probability-labels">
        <span>{percent(home_win)} home</span>
        <span>{percent(draw)} draw</span>
        <span>{percent(away_win)} away</span>
      </div>
      <div className="probability-bar" aria-label="Result probabilities">
        <span className="home" style={{ width: `${home_win * 100}%` }} />
        <span className="draw" style={{ width: `${draw * 100}%` }} />
        <span className="away" style={{ width: `${away_win * 100}%` }} />
      </div>
    </div>
  );
}

export function MatchCard({ match }: { match: Match }) {
  return (
    <Link className="match-card" to={`/match/${match.id}`}>
      <div className="eyebrow">
        Group {match.group} · Match {match.number}
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
            <span>{match.prediction.confidence}</span>
          </div>
        </>
      )}
    </Link>
  );
}
