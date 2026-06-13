import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import App from "./App";
import type { Match } from "./types";

vi.stubEnv("VITE_API_URL", "https://example.test");

const completedMatch = {
  id: "WC26-001",
  number: 1,
  stage: "group",
  kickoff: "2026-06-11T17:00:00+00:00",
  venue_id: "MEX",
  group: "A",
  home_team: { id: "MEX", name: "Mexico", flag: "🇲🇽" },
  away_team: { id: "RSA", name: "South Africa", flag: "🇿🇦" },
  home_slot: null,
  away_slot: null,
  status: "finished",
  home_score: 2,
  away_score: 1,
  prediction: null,
} as Match;

describe("Results page", () => {
  it("is routed and displays completed match scores", () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { staleTime: Infinity } },
    });
    queryClient.setQueryData(["matches"], [completedMatch]);

    const html = renderToString(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/results"]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(html).toContain("Results");
    expect(html).toContain("Mexico");
    expect(html).toContain("South Africa");
    expect(html).toContain("2");
    expect(html).toContain("1");
    expect(html).toContain('href="/results"');
  });

  it("shows an empty state when no results are available", () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { staleTime: Infinity } },
    });
    queryClient.setQueryData(["matches"], []);

    const html = renderToString(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/results"]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(html).toContain("No completed matches are available yet.");
  });
});
