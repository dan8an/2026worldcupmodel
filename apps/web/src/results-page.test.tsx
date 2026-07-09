import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-06-12T18:00:00+00:00"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

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

  it("hides kicked-off scheduled matches without final scores", () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { staleTime: Infinity } },
    });
    queryClient.setQueryData(["matches"], [
      completedMatch,
      {
        ...completedMatch,
        id: "past-unscored",
        number: 9,
        kickoff: "2026-06-12T17:00:00+00:00",
        status: "scheduled",
        home_score: null,
        away_score: null,
        group: "B",
        home_slot: "Past Home",
        away_slot: "Past Away",
        home_team: null,
        away_team: null,
      },
      {
        ...completedMatch,
        id: "future",
        number: 3,
        kickoff: "2026-06-12T20:00:00+00:00",
        status: "scheduled",
        home_score: null,
        away_score: null,
        home_slot: "Future Home",
        away_slot: "Future Away",
        home_team: null,
        away_team: null,
      },
    ]);

    const html = renderToString(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/results"]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const normalizedHtml = html.replaceAll("<!-- -->", "");

    expect(normalizedHtml).not.toContain("Match 2");
    expect(normalizedHtml).not.toContain("Past Home");
    expect(normalizedHtml).not.toContain("Awaiting final score.");
    expect(normalizedHtml).not.toContain("Future Home");
    expect(normalizedHtml).toContain("Mexico");
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

  it("shows completed knockout matches in Results with resolved team labels", () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { staleTime: Infinity } },
    });
    queryClient.setQueryData(["matches"], [
      {
        ...completedMatch,
        id: "WC26-073",
        number: 73,
        stage: "round_of_32",
        group: null,
        kickoff: "2026-06-28T16:00:00+00:00",
        status: "finished",
        home_score: 3,
        away_score: 1,
      },
    ]);

    const html = renderToString(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/results"]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    ).replaceAll("<!-- -->", "");

    expect(html).toContain("Round of 32");
    expect(html).toContain("Mexico");
    expect(html).toContain("MEX");
    expect(html).not.toContain("Group null");
  });
});
