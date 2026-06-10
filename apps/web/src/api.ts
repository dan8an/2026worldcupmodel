import type { Match, ModelPerformance, Simulation, Team, TeamProfile } from "./types";

const configuredApiUrl = import.meta.env.VITE_API_URL;

if (!configuredApiUrl) {
  throw new Error(
    "VITE_API_URL is not configured. Add it to apps/web/.env.local or the Vercel environment.",
  );
}

const API_URL = configuredApiUrl.replace(/\/+$/, "");

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}/api${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...options?.headers },
  });
  if (!response.ok) {
    throw new Error(`API request failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  matches: () => request<Match[]>("/matches?stage=group"),
  match: (id: string) => request<Match>(`/matches/${id}`),
  teams: () => request<Team[]>("/teams"),
  team: (id: string) => request<TeamProfile>(`/teams/${id}`),
  simulation: () => request<Simulation>("/simulations/latest"),
  performance: () => request<ModelPerformance>("/model/performance"),
  customSimulation: (iterations: number, seed: number) =>
    request<Simulation>("/simulations/custom", {
      method: "POST",
      body: JSON.stringify({ iterations, seed }),
    }),
};
