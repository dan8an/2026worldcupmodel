import { describe, expect, it } from "vitest";
import {
  championshipOutlookTeams,
  simulationDriverLabels,
  simulationSignalBadges,
} from "./simulation-display";
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

describe("Championship outlook model transparency", () => {
  it("labels high shot volume and limited rating history professionally", () => {
    const team = {
      team_id: "NZL",
      team_name: "New Zealand",
      champion: 0.01,
      model_inputs: {
        elo_rating: 1450,
        elo_rank: 42,
        attack_rating: 50,
        defense_rating: 50,
        shot_volume_rating: 97,
        rating_source: "canonical_rank_prior",
        rating_matches: 0,
        shot_volume_sample_matches: 3,
      },
    } as SimulationTeam;

    expect(simulationSignalBadges(team)).toEqual([
      "High shot-volume signal",
      "Limited rating history",
    ]);
    expect(simulationDriverLabels(team)).toEqual([
      "Elo #42",
      "Attack 50.0",
      "Defense 50.0",
      "Shot volume 97.0",
      "Source: conservative prior",
    ]);
  });

  it("marks small database samples without presenting them as failures", () => {
    const team = {
      team_id: "NOR",
      team_name: "Norway",
      champion: 0.07,
      model_inputs: {
        elo_rating: 1677.64,
        elo_rank: 12,
        attack_rating: 85.56,
        defense_rating: 73.33,
        shot_volume_rating: 100,
        rating_source: "database_current",
        rating_matches: 4,
        shot_volume_sample_matches: 12,
      },
    } as SimulationTeam;

    expect(simulationSignalBadges(team)).toEqual([
      "High shot-volume signal",
      "Limited data",
    ]);
  });
});
