import type { SimulationTeam, Team } from "./types";

export function simulationSignalBadges(team: SimulationTeam): string[] {
  const inputs = team.model_inputs;
  if (!inputs) return [];

  const badges = [];
  if (
    inputs.shot_volume_rating != null &&
    inputs.shot_volume_rating >= 95
  ) {
    badges.push("High shot-volume signal");
  }
  if (inputs.rating_source === "canonical_rank_prior") {
    badges.push("Limited rating history");
  } else if (
    inputs.rating_matches < 5 ||
    (
      inputs.shot_volume_sample_matches != null &&
      inputs.shot_volume_sample_matches < 5
    )
  ) {
    badges.push("Limited data");
  }
  return badges;
}

export function simulationDriverLabels(team: SimulationTeam): string[] {
  const inputs = team.model_inputs;
  if (!inputs) return [];

  const labels = [
    `Elo #${inputs.elo_rank}`,
    `Attack ${inputs.attack_rating.toFixed(1)}`,
    `Defense ${inputs.defense_rating.toFixed(1)}`,
  ];
  if (inputs.shot_volume_rating != null) {
    labels.push(`Shot volume ${inputs.shot_volume_rating.toFixed(1)}`);
  }
  labels.push(
    inputs.rating_source === "canonical_rank_prior"
      ? "Source: conservative prior"
      : "Source: current ratings",
  );
  return labels;
}

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
