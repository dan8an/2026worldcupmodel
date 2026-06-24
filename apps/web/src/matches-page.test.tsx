import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import type { Match } from "./types";

vi.stubEnv("VITE_API_URL", "https://example.test");

const matchFixture = (overrides: Partial<Match>): Match => ({
  id: "WC26-001",
  number: 1,
  stage: "group",
  kickoff: "2026-06-11T17:00:00+00:00",
  venue_id: "MEX",
  group: "A",
  home_team: null,
  away_team: null,
  home_slot: "Home",
  away_slot: "Away",
  status: "scheduled",
  home_score: null,
  away_score: null,
  prediction: null,
  ...overrides,
});

describe("Matches page", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-06-11T16:00:00+00:00"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("numbers matches by chronological order instead of canonical group order", () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { staleTime: Infinity } },
    });
    queryClient.setQueryData(["matches"], [
      matchFixture({
        id: "WC26-001",
        number: 1,
        kickoff: "2026-06-11T17:00:00+00:00",
        group: "A",
      }),
      matchFixture({
        id: "WC26-009",
        number: 9,
        kickoff: "2026-06-12T17:00:00+00:00",
        group: "B",
      }),
      matchFixture({
        id: "WC26-003",
        number: 3,
        kickoff: "2026-06-18T17:00:00+00:00",
        group: "A",
      }),
    ]);

    const html = renderToString(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/matches"]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const normalizedHtml = html.replaceAll("<!-- -->", "");

    expect(normalizedHtml).toContain("Group A · Match 1");
    expect(normalizedHtml).toContain("Group B · Match 2");
    expect(normalizedHtml).toContain("Group A · Match 3");
    expect(normalizedHtml).not.toContain("Group B · Match 9");
  });

  it("hides past/completed matches and keeps full-schedule chronological numbers", () => {
    vi.setSystemTime(new Date("2026-06-12T18:00:00+00:00"));
    const queryClient = new QueryClient({
      defaultOptions: { queries: { staleTime: Infinity } },
    });
    queryClient.setQueryData(["matches"], [
      matchFixture({
        id: "completed",
        number: 1,
        kickoff: "2026-06-11T17:00:00+00:00",
        group: "A",
        status: "completed",
        home_score: 2,
        away_score: 0,
        home_slot: "Completed Home",
      }),
      matchFixture({
        id: "past",
        number: 2,
        kickoff: "2026-06-12T17:00:00+00:00",
        group: "A",
        home_slot: "Past Home",
      }),
      matchFixture({
        id: "future",
        number: 9,
        kickoff: "2026-06-12T20:00:00+00:00",
        group: "B",
        home_slot: "Future Home",
      }),
    ]);

    const html = renderToString(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/matches"]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const normalizedHtml = html.replaceAll("<!-- -->", "");

    expect(normalizedHtml).not.toContain("Completed Home");
    expect(normalizedHtml).not.toContain("Past Home");
    expect(normalizedHtml).toContain("Future Home");
    expect(normalizedHtml).toContain("Group B · Match 3");
    expect(normalizedHtml).not.toContain("Group B · Match 9");
  });
});
