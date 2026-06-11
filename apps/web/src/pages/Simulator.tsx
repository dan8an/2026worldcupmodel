import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { ErrorState, Loading, percent } from "../components";
import { rankSimulationTeams } from "../simulation-display";

export function Simulator() {
  const baseline = useQuery({ queryKey: ["simulation"], queryFn: api.simulation });
  const custom = useMutation({ mutationFn: ({ iterations, seed }: { iterations: number; seed: number }) =>
    api.customSimulation(iterations, seed) });
  const [iterations, setIterations] = useState(1000);
  const [seed, setSeed] = useState(2026);
  if (baseline.isLoading) return <Loading label="Simulating tournament" />;
  if (baseline.isError || !baseline.data) return <ErrorState />;
  const data = custom.data ?? baseline.data;
  const rankedTeams = rankSimulationTeams(data.teams);
  return (
    <section>
      <div className="page-heading">
        <span className="eyebrow">Reproducible Monte Carlo</span>
        <h1>Tournament simulator</h1>
        <p>Rerun the tournament with a chosen seed and up to 10,000 iterations.</p>
      </div>
      <form
        className="sim-controls"
        onSubmit={(event) => {
          event.preventDefault();
          custom.mutate({ iterations, seed });
        }}
      >
        <label>Iterations<input type="number" min="1" max="10000" value={iterations}
          onChange={(event) => setIterations(Number(event.target.value))} /></label>
        <label>Random seed<input type="number" value={seed}
          onChange={(event) => setSeed(Number(event.target.value))} /></label>
        <button className="button primary" disabled={custom.isPending}>
          {custom.isPending ? "Running..." : "Run simulation"}
        </button>
      </form>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Team</th><th>R32</th><th>Quarterfinal</th><th>Semifinal</th><th>Final</th><th>Champion</th></tr></thead>
          <tbody>
            {rankedTeams.map((team) => (
              <tr key={team.team_id}>
                <td><strong>{team.team_name}</strong></td>
                <td>{percent(team.round_of_32)}</td>
                <td>{percent(team.quarterfinal)}</td>
                <td>{percent(team.semifinal)}</td>
                <td>{percent(team.final)}</td>
                <td><b>{percent(team.champion)}</b></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="disclosure">
        Current pairing implementation uses deterministic strength seeding after
        selecting the best third-place teams. Official Annex C mapping remains a launch gate.
        Published probabilities use 50,000 iterations; custom runs are capped at
        10,000 to remain responsive.
      </p>
    </section>
  );
}
