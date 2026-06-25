import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api";
import { ErrorState, Loading, MatchCard, percent } from "../components";
import {
  championshipOutlookTeams,
  simulationDriverLabels,
  simulationSignalBadges,
} from "../simulation-display";
import { matchSchedule } from "../match-status";

export function Dashboard() {
  const matches = useQuery({ queryKey: ["matches"], queryFn: api.matches });
  const simulation = useQuery({ queryKey: ["simulation"], queryFn: api.simulation });
  const teams = useQuery({ queryKey: ["teams"], queryFn: api.teams });

  if (matches.isLoading || simulation.isLoading || teams.isLoading) return <Loading />;
  if (matches.isError || simulation.isError || teams.isError) return <ErrorState />;

  const matchData = matches.data ?? [];
  const schedule = matchSchedule(matchData);
  const simulationData = simulation.data;
  if (!simulationData) return <ErrorState />;
  const favorites = championshipOutlookTeams(
    simulationData.teams,
    teams.data ?? [],
  );
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
          <Link to="/matches">View upcoming matches</Link>
        </div>
        <div className="card-grid">
          {schedule.upcoming.slice(0, 6).map((match) => (
            <MatchCard
              key={match.id}
              match={match}
              displayNumber={schedule.numberById.get(match.id)}
            />
          ))}
        </div>
        {schedule.upcoming.length === 0 && (
          <div className="state-card empty-state">
            No active matches are available. Final scores are listed in Results.
          </div>
        )}
      </section>

      <section>
        <div className="section-heading">
          <div><span className="eyebrow">Tournament model</span><h2>Championship outlook</h2></div>
          <Link to="/simulator">Open simulator</Link>
        </div>
        <aside className="model-notes" aria-label="Championship outlook model notes">
          <strong>Model notes</strong>
          <span>
            Tournament odds are simulated from match probabilities. Shot-volume
            v4 can boost teams with strong recent attacking volume, so read
            title odds alongside model confidence and the input notes below.
          </span>
        </aside>
        <div className="ranking-card">
          {favorites.map((team, index) => {
            const drivers = simulationDriverLabels(team);
            const badges = simulationSignalBadges(team);
            return (
              <Link to={`/teams/${team.team_id}`} className="ranking-row" key={team.team_id}>
                <span className="rank">{String(index + 1).padStart(2, "0")}</span>
                <div className="ranking-team">
                  <strong>{team.team_name}</strong>
                  {drivers.length > 0 && (
                    <small className="model-drivers">{drivers.join(" · ")}</small>
                  )}
                  {badges.length > 0 && (
                    <span className="model-badges">
                      {badges.map((badge) => (
                        <small className="model-badge" key={badge}>{badge}</small>
                      ))}
                    </span>
                  )}
                </div>
                <div className="mini-track">
                  <span style={{ width: `${team.champion * 100}%` }} />
                </div>
                <b>{percent(team.champion)}</b>
              </Link>
            );
          })}
        </div>
      </section>
    </>
  );
}
