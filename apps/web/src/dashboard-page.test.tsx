import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import App from "./App";
import type { Match, Simulation } from "./types";

vi.stubEnv("VITE_API_URL", "https://example.test");

const completedMatch = {
  id: "WC26-001",
  number: 1,
  stage: "group",
  kickoff: "2026-06-11T17:00:00+00:00",
  venue_id: "MEX",
  group: "A",
  home_team: null,
  away_team: null,
  home_slot: "Mexico",
  away_slot: "South Africa",
  status: "finished",
  home_score: 2,
  away_score: 1,
  prediction: null,
} as Match;

const simulation = {
  iterations: 50_000,
  seed: 2026,
  model_version: "test",
  generated_at: "2026-06-12T00:00:00Z",
  created_at: "2026-06-12T00:00:00Z",
  data_cutoff: "2026-06-12T00:00:00Z",
  source: "database_latest",
  monte_carlo_precision: {
    worst_case_standard_error: 0.002,
    worst_case_95_margin: 0.004,
  },
  teams: [],
} as Simulation;

describe("Dashboard match filtering", () => {
  it("hides completed matches and shows the upcoming empty state", () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { staleTime: Infinity } },
    });
    queryClient.setQueryData(["matches"], [completedMatch]);
    queryClient.setQueryData(["simulation"], simulation);
    queryClient.setQueryData(["teams"], []);

    const html = renderToString(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/"]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(html).toContain("No upcoming matches are available.");
    expect(html).not.toContain("Mexico<!-- -->");
    expect(html).not.toContain("South Africa<!-- -->");
  });
});
