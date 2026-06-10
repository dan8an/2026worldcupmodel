import { beforeEach, describe, expect, it, vi } from "vitest";

describe("API client routes", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.stubEnv("VITE_API_URL", "https://two026worldcupmodel.onrender.com/");
  });

  it("uses the backend v1 routes", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => [],
    });
    vi.stubGlobal("fetch", fetchMock);

    const { api } = await import("./api");
    await api.teams();
    await api.matches();
    await api.simulation();

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "https://two026worldcupmodel.onrender.com/v1/teams",
      "https://two026worldcupmodel.onrender.com/v1/matches?stage=group",
      "https://two026worldcupmodel.onrender.com/v1/simulations/latest",
    ]);
  });
});
