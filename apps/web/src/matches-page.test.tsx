import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
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
});
