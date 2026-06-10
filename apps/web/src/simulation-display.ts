import type { SimulationTeam, Team } from "./types";

export function championshipOutlookTeams(
  simulationTeams: SimulationTeam[],
  teams: Team[],
  limit = 8,
): SimulationTeam[] {
  const teamsById = new Map(teams.map((team) => [team.id, team]));

  return [...simulationTeams]
    .sort((left, right) => right.champion - left.champion)
    .slice(0, limit)
    .map((result) => {
      const team = teamsById.get(result.team_id);
      return {
        ...result,
        team_name: result.team_name?.trim() || team?.name || result.team_id,
        flag: result.flag || team?.flag,
        group: result.group || team?.group,
        confederation: result.confederation || team?.confederation,
      };
    });
}
