import type { Match, ModelPerformance, Simulation, Team, TeamProfile } from "./types";

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...options?.headers },
  });
  if (!response.ok) {
    throw new Error(`API request failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  matches: () => request<Match[]>("/v1/matches?stage=group"),
  match: (id: string) => request<Match>(`/v1/matches/${id}`),
  teams: () => request<Team[]>("/v1/teams"),
  team: (id: string) => request<TeamProfile>(`/v1/teams/${id}`),
  simulation: () => request<Simulation>("/v1/simulations/latest"),
  performance: () => request<ModelPerformance>("/v1/model/performance"),
  customSimulation: (iterations: number, seed: number) =>
    request<Simulation>("/v1/simulations/custom", {
      method: "POST",
      body: JSON.stringify({ iterations, seed }),
    }),
};
