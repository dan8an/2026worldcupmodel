import { describe, expect, it } from "vitest";
import { championshipOutlookTeams } from "./simulation-display";
import type { SimulationTeam, Team } from "./types";

describe("championshipOutlookTeams", () => {
  it("fills every Championship outlook label from canonical teams", () => {
    const teams: Team[] = [
      {
        id: "ARG",
        name: "Argentina",
        group: "J",
        position: 1,
        rank: 2,
        host: false,
        elo: 2100,
        flag: "🇦🇷",
        confederation: "CONMEBOL",
      },
      {
        id: "ESP",
        name: "Spain",
        group: "H",
        position: 1,
        rank: 1,
        host: false,
        elo: 2110,
        flag: "🇪🇸",
        confederation: "UEFA",
      },
    ];
    const results = [
      { team_id: "ARG", team_name: "", champion: 0.2 },
      { team_id: "ESP", team_name: "", champion: 0.25 },
    ] as SimulationTeam[];

    const outlook = championshipOutlookTeams(results, teams);

    expect(outlook.map((team) => team.team_name)).toEqual(["Spain", "Argentina"]);
    expect(outlook.every((team) => team.team_name.trim().length > 0)).toBe(true);
  });
});
