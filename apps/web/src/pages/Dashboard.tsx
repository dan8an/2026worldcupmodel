import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api";
import { ErrorState, Loading, MatchCard, percent } from "../components";

export function Dashboard() {
  const matches = useQuery({ queryKey: ["matches"], queryFn: api.matches });
  const simulation = useQuery({ queryKey: ["simulation"], queryFn: api.simulation });

  if (matches.isLoading || simulation.isLoading) return <Loading />;
  if (matches.isError || simulation.isError) return <ErrorState />;

  const matchData = matches.data ?? [];
  const simulationData = simulation.data;
  if (!simulationData) return <ErrorState />;
  const favorites = simulationData.teams.slice(0, 8);
  return (
    <>
      <section className="hero">
        <div>
          <span className="eyebrow">2026 tournament intelligence</span>
          <h1>Every path. Measured.</h1>
          <p>
            Match probabilities, expected goals, and tournament paths from a
            transparent context-adjusted Poisson model.
          </p>
          <div className="hero-actions">
            <Link className="button primary" to="/matches">Explore matches</Link>
            <Link className="button" to="/model-explainer">How it works</Link>
          </div>
        </div>
        <div className="hero-stat">
          <span>Simulations</span>
          <strong>{simulationData.iterations.toLocaleString()}</strong>
          <small>
            Seed {simulationData.seed} · worst-case sampling margin ±
            {(simulationData.monte_carlo_precision.worst_case_95_margin * 100).toFixed(2)} pts
          </small>
        </div>
      </section>

      <section>
        <div className="section-heading">
          <div><span className="eyebrow">Opening slate</span><h2>Next matches</h2></div>
          <Link to="/matches">View all 72 group matches</Link>
        </div>
        <div className="card-grid">
          {matchData.slice(0, 6).map((match) => <MatchCard key={match.id} match={match} />)}
        </div>
      </section>

      <section>
        <div className="section-heading">
          <div><span className="eyebrow">Tournament model</span><h2>Championship outlook</h2></div>
          <Link to="/simulator">Open simulator</Link>
        </div>
        <div className="ranking-card">
          {favorites.map((team, index) => (
            <Link to={`/teams/${team.team_id}`} className="ranking-row" key={team.team_id}>
              <span className="rank">{String(index + 1).padStart(2, "0")}</span>
              <strong>{team.team_name}</strong>
              <div className="mini-track">
                <span style={{ width: `${team.champion * 100}%` }} />
              </div>
              <b>{percent(team.champion)}</b>
            </Link>
          ))}
        </div>
      </section>
    </>
  );
}
