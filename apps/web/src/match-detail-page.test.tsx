import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import App from "./App";
import type { Match } from "./types";

vi.stubEnv("VITE_API_URL", "https://example.test");

const knockoutMatch: Match = {
  id: "provider-qf-90101",
  number: 97,
  stage: "quarterfinal",
  kickoff: "2026-07-09T20:00:00+00:00",
  venue_id: "TBD",
  group: null,
  home_team: {
    id: "MEX",
    name: "Mexico",
    group: "A",
    position: 1,
    rank: 14,
    host: true,
    elo: 1900,
    flag: "🇲🇽",
  },
  away_team: {
    id: "RSA",
    name: "South Africa",
    group: "A",
    position: 2,
    rank: 55,
    host: false,
    elo: 1700,
    flag: "🇿🇦",
  },
  home_slot: null,
  away_slot: null,
  status: "scheduled",
  home_score: null,
  away_score: null,
  prediction: null,
};

describe("Match detail prediction states", () => {
  it("labels historical backfills clearly", () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { staleTime: Infinity } },
    });
    queryClient.setQueryData(["match", "provider-qf-90101"], {
        ...knockoutMatch,
        prediction: {
          match_id: "ko", home_team_id: "USA", away_team_id: "MEX",
          home_xg: 1.2, away_xg: 1.1,
          probabilities: { home_win: .4, draw: .3, away_win: .3 },
          top_scores: [], confidence: "Medium", key_factors: [], top_factors: [],
          context: { home_form_elo: 0, away_form_elo: 0, home_h2h_elo: 0, away_h2h_elo: 0, home_availability_elo: 0, away_availability_elo: 0, historical_matches_home: 0, historical_matches_away: 0, h2h_matches: 0, availability_reports: 0, data_cutoff: null },
          model_version: "elo-context-v4.2.1", generated_at: "2026-07-10T00:00:00Z", data_cutoff: "2026-07-09T19:59:59Z",
          generation_mode: "historical_backfill",
        },
    });
    const html = renderToString(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/match/provider-qf-90101"]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(html).toContain("Backfilled pre-match prediction");
  });
  it("shows a clear missing-prediction state without API unavailable copy", () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { staleTime: Infinity } },
    });
    queryClient.setQueryData(["match", "provider-qf-90101"], knockoutMatch);

    const html = renderToString(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/match/provider-qf-90101"]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    ).replaceAll("<!-- -->", "");

    expect(html).toContain("Quarterfinal · Match 97");
    expect(html).toContain("Prediction not available yet.");
    expect(html).not.toContain("The forecast API is unavailable");
    expect(html).not.toContain("Group null");
  });

  it("shows final score for a completed knockout match without prediction", () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { staleTime: Infinity } },
    });
    queryClient.setQueryData(["match", "provider-qf-90101"], {
      ...knockoutMatch,
      status: "FT",
      home_score: 2,
      away_score: 0,
    });

    const html = renderToString(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/match/provider-qf-90101"]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    ).replaceAll("<!-- -->", "");

    expect(html).toContain("Final score");
    expect(html).toContain("2 : 0");
    expect(html).toContain("Prediction not available yet.");
    expect(html).not.toContain("The forecast API is unavailable");
  });
});
