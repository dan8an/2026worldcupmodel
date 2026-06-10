import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api";
import { ErrorState, Loading, percent } from "../components";

export function Teams() {
  const teams = useQuery({ queryKey: ["teams"], queryFn: api.teams });
  const simulation = useQuery({ queryKey: ["simulation"], queryFn: api.simulation });
  if (teams.isLoading || simulation.isLoading) return <Loading label="Loading teams" />;
  if (teams.isError || simulation.isError || !teams.data || !simulation.data) {
    return <ErrorState />;
  }
  const probabilities = new Map(
    simulation.data.teams.map((team) => [team.team_id, team]),
  );
  return (
    <section>
      <div className="page-heading">
        <span className="eyebrow">48 intelligence profiles</span>
        <h1>Teams</h1>
        <p>
          Open any team for recent results, key players, group-match probabilities,
          tournament goals, and grounded analysis.
        </p>
      </div>
      <div className="team-directory">
        {teams.data.map((team) => {
          const outlook = probabilities.get(team.id);
          return (
            <Link className="team-directory-card" to={`/teams/${team.id}`} key={team.id}>
              <span className="eyebrow">Group {team.group}</span>
              <h2><span className="flag directory-flag" aria-hidden="true">{team.flag}</span>{team.name}</h2>
              <div>
                <span>R32 {outlook ? percent(outlook.round_of_32) : "—"}</span>
                <span>Title {outlook ? percent(outlook.champion) : "—"}</span>
              </div>
            </Link>
          );
        })}
      </div>
    </section>
  );
}
