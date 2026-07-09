import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import App from "./App";
import type { Match } from "./types";

vi.stubEnv("VITE_API_URL", "https://example.test");

const team = (id: string, name: string) => ({
  id,
  name,
  group: "A",
  position: 1,
  rank: 1,
  host: false,
  elo: 2000,
  flag: "",
});

const match = (overrides: Partial<Match>): Match => ({
  id: "provider-73",
  number: 73,
  stage: "round_of_32",
  kickoff: "2026-06-28T16:00:00+00:00",
  venue_id: "TBD",
  group: null,
  home_team: team("MEX", "Mexico"),
  away_team: team("RSA", "South Africa"),
  home_slot: null,
  away_slot: null,
  status: "scheduled",
  home_score: null,
  away_score: null,
  prediction: null,
  ...overrides,
});

describe("Bracket page", () => {
  it("groups knockout matches by stage and renders final scores", () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { staleTime: Infinity } },
    });
    queryClient.setQueryData(["matches"], [
      match({ id: "group", number: 1, stage: "group", group: "A" }),
      match({
        id: "provider-73",
        number: 73,
        stage: "round_of_32",
        status: "finished",
        home_score: 2,
        away_score: 1,
      }),
      match({
        id: "provider-103",
        number: 103,
        stage: "third_place",
        kickoff: "2026-07-18T20:00:00+00:00",
        home_team: null,
        away_team: null,
        home_slot: "Loser Match 101",
        away_slot: "Loser Match 102",
      }),
    ]);

    const html = renderToString(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/bracket"]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    ).replaceAll("<!-- -->", "");

    expect(html).toContain("Round of 32");
    expect(html).toContain("Third-place");
    expect(html).toContain("Match 73");
    expect(html).toContain("Mexico");
    expect(html).toContain(">2</span>");
    expect(html).toContain(">1</span>");
    expect(html).toContain("Loser Match 101");
    expect(html).not.toContain("<small>Match 1</small>");
  });
});
