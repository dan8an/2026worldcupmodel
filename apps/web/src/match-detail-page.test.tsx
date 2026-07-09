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
});
