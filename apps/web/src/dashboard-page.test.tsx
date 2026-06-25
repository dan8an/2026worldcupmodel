import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-06-11T16:00:00+00:00"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("hides completed matches and shows the active-match empty state", () => {
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

    expect(html).toContain("No active matches are available.");
    expect(html).not.toContain("Mexico<!-- -->");
    expect(html).not.toContain("South Africa<!-- -->");
  });

  it("shows future and in-progress matches, hides final results, and keeps full-schedule chronological numbers", () => {
    vi.setSystemTime(new Date("2026-06-12T18:00:00+00:00"));
    const queryClient = new QueryClient({
      defaultOptions: { queries: { staleTime: Infinity } },
    });
    queryClient.setQueryData(["matches"], [
      {
        ...completedMatch,
        id: "completed",
        number: 1,
        kickoff: "2026-06-11T17:00:00+00:00",
        status: "completed",
        home_slot: "Completed Home",
        away_slot: "Completed Away",
      },
      {
        ...completedMatch,
        id: "past-unscored",
        number: 2,
        kickoff: "2026-06-12T17:00:00+00:00",
        status: "scheduled",
        home_score: null,
        away_score: null,
        home_slot: "Past Home",
        away_slot: "Past Away",
      },
      {
        ...completedMatch,
        id: "future",
        number: 9,
        kickoff: "2026-06-12T20:00:00+00:00",
        status: "scheduled",
        home_score: null,
        away_score: null,
        group: "B",
        home_slot: "Future Home",
        away_slot: "Future Away",
      },
    ]);
    queryClient.setQueryData(["simulation"], simulation);
    queryClient.setQueryData(["teams"], []);

    const html = renderToString(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/"]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const normalizedHtml = html.replaceAll("<!-- -->", "");

    expect(normalizedHtml).not.toContain("Completed Home");
    expect(normalizedHtml).toContain("Past Home");
    expect(normalizedHtml).toContain("In progress");
    expect(normalizedHtml).toContain("Future Home");
    expect(normalizedHtml).toContain("Group A · Match 2");
    expect(normalizedHtml).toContain("Group B · Match 3");
    expect(normalizedHtml).not.toContain("Group B · Match 9");
  });
});
