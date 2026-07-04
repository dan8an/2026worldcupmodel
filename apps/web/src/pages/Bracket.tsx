import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { ErrorState, Loading, TeamLabel } from "../components";

const rounds = [
  ["round_of_32", "Round of 32"],
  ["round_of_16", "Round of 16"],
  ["quarterfinal", "Quarterfinals"],
  ["semifinal", "Semifinals"],
  ["final", "Final"],
] as const;

export function Bracket() {
  const query = useQuery({ queryKey: ["matches"], queryFn: api.matches });
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
            {(query.data ?? []).filter((match) => match.stage === stage).map((match) => (
              <article className="match-card bracket-match" key={match.id}>
                <small>Match {match.number}</small>
                <strong><TeamLabel team={match.home_team} placeholder={match.home_slot} /></strong>
                <strong><TeamLabel team={match.away_team} placeholder={match.away_slot} /></strong>
              </article>
            ))}
          </div>
        ))}
      </div>
    </section>
  );
}
