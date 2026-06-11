const numberOrNull = (value) => {
  if (value == null || value === "") return null;
  const parsed = Number(String(value).replace("%", ""));
  return Number.isFinite(parsed) ? parsed : null;
};

const integerOrNull = (value) => {
  const parsed = numberOrNull(value);
  return parsed == null ? null : Math.round(parsed);
};

const statistic = (statistics, ...names) => {
  for (const name of names) {
    if (statistics[name] != null) return statistics[name];
  }
  return null;
};

export class SportsIngestionRepository {
  constructor(pool, { logger = console } = {}) {
    this.pool = pool;
    this.logger = logger;
  }

  async assertSchema() {
    const result = await this.pool.query(`
      select
        to_regclass('public.players') as players,
        to_regclass('public.team_match_stats') as team_match_stats,
        to_regclass('public.player_match_stats') as player_match_stats,
        exists (
          select 1
          from information_schema.columns
          where table_schema = 'public'
            and table_name = 'matches'
            and column_name = 'api_football_fixture_id'
        ) as has_fixture_provider_id,
        (
          select count(*) = 5
          from information_schema.columns
          where table_schema = 'public'
            and table_name = 'team_match_stats'
            and column_name in (
              'shots_inside_box',
              'shots_outside_box',
              'blocked_shots',
              'goalkeeper_saves',
              'pass_accuracy'
            )
        ) as has_xg_proxy_fields
    `);
    const schema = result.rows[0];
    if (
      !schema.players ||
      !schema.team_match_stats ||
      !schema.player_match_stats ||
      !schema.has_fixture_provider_id ||
      !schema.has_xg_proxy_fields
    ) {
      throw new Error(
        "Daily pipeline schema is missing. Apply " +
        "supabase/migrations/202606100001_daily_prediction_pipeline.sql and " +
        "supabase/migrations/202606110003_xg_proxy_v4.sql first.",
      );
    }
  }

  async upsertTeam(client, team) {
    const existing = await client.query(
      `
        select id
        from public.teams
        where api_football_team_id = $1
           or lower(name) = lower($2)
        order by (api_football_team_id = $1) desc
        limit 1
      `,
      [team.providerId, team.name],
    );

    if (existing.rows[0]) {
      await client.query(
        `
          update public.teams
          set api_football_team_id = coalesce(api_football_team_id, $1)
          where id = $2
        `,
        [team.providerId, existing.rows[0].id],
      );
      return existing.rows[0].id;
    }

    const inserted = await client.query(
      `
        insert into public.teams (name, api_football_team_id)
        values ($1, $2)
        returning id
      `,
      [team.name, team.providerId],
    );
    return inserted.rows[0].id;
  }

  async upsertMatch(client, fixture, homeTeamId, awayTeamId) {
    const result = await client.query(
      `
        insert into public.matches (
          home_team,
          away_team,
          match_date,
          tournament_stage,
          home_score,
          away_score,
          completed,
          home_team_id,
          away_team_id,
          api_football_fixture_id,
          provider_name,
          provider_payload,
          updated_at
        )
        values ($1, $2, $3, $4, $5, $6, true, $7, $8, $9, 'api_football', $10, now())
        on conflict (api_football_fixture_id) where api_football_fixture_id is not null
        do update set
          home_team = excluded.home_team,
          away_team = excluded.away_team,
          match_date = excluded.match_date,
          tournament_stage = excluded.tournament_stage,
          home_score = excluded.home_score,
          away_score = excluded.away_score,
          completed = true,
          home_team_id = excluded.home_team_id,
          away_team_id = excluded.away_team_id,
          provider_name = excluded.provider_name,
          provider_payload = excluded.provider_payload,
          updated_at = now()
        returning id
      `,
      [
        fixture.homeTeam.name,
        fixture.awayTeam.name,
        fixture.date,
        fixture.round ?? fixture.competition,
        fixture.homeScore,
        fixture.awayScore,
        homeTeamId,
        awayTeamId,
        fixture.providerFixtureId,
        JSON.stringify(fixture.raw ?? {}),
      ],
    );
    return result.rows[0].id;
  }

  async upsertPlayer(client, player, teamId, position) {
    const providerKey = `api_football:${player.providerId}`;
    const result = await client.query(
      `
        insert into public.players (
          team_id,
          provider_key,
          display_name,
          primary_position,
          updated_at
        )
        values ($1, $2, $3, $4, now())
        on conflict (provider_key) where provider_key is not null
        do update set
          team_id = excluded.team_id,
          display_name = excluded.display_name,
          primary_position = coalesce(excluded.primary_position, public.players.primary_position),
          active = true,
          updated_at = now()
        returning id
      `,
      [teamId, providerKey, player.name, position],
    );
    return result.rows[0].id;
  }

  async upsertTeamStats(
    client,
    { fixture, matchId, teamId, opponentTeamId, isHome, statistics },
  ) {
    await client.query(
      `
        insert into public.team_match_stats (
          match_id,
          team_id,
          opponent_team_id,
          is_home,
          goals,
          expected_goals,
          possession,
          shots,
          shots_on_target,
          shots_inside_box,
          shots_outside_box,
          blocked_shots,
          goalkeeper_saves,
          corners,
          fouls,
          yellow_cards,
          red_cards,
          passes_attempted,
          passes_completed,
          pass_accuracy,
          source_name,
          source_match_key,
          captured_at,
          raw_payload
        )
        values (
          $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
          $11, $12, $13, $14, $15, $16, $17, $18, $19, $20,
          'api_football', $21, now(), $22
        )
        on conflict (match_id, team_id)
        do update set
          opponent_team_id = excluded.opponent_team_id,
          is_home = excluded.is_home,
          goals = excluded.goals,
          expected_goals = excluded.expected_goals,
          possession = excluded.possession,
          shots = excluded.shots,
          shots_on_target = excluded.shots_on_target,
          shots_inside_box = excluded.shots_inside_box,
          shots_outside_box = excluded.shots_outside_box,
          blocked_shots = excluded.blocked_shots,
          goalkeeper_saves = excluded.goalkeeper_saves,
          corners = excluded.corners,
          fouls = excluded.fouls,
          yellow_cards = excluded.yellow_cards,
          red_cards = excluded.red_cards,
          passes_attempted = excluded.passes_attempted,
          passes_completed = excluded.passes_completed,
          pass_accuracy = excluded.pass_accuracy,
          captured_at = excluded.captured_at,
          raw_payload = excluded.raw_payload
      `,
      [
        matchId,
        teamId,
        opponentTeamId,
        isHome,
        isHome ? fixture.homeScore : fixture.awayScore,
        numberOrNull(statistic(statistics.statistics, "expected_goals", "Expected Goals")),
        numberOrNull(statistic(statistics.statistics, "Ball Possession")),
        integerOrNull(statistic(statistics.statistics, "Total Shots")),
        integerOrNull(statistic(statistics.statistics, "Shots on Goal")),
        integerOrNull(statistic(statistics.statistics, "Shots insidebox")),
        integerOrNull(statistic(statistics.statistics, "Shots outsidebox")),
        integerOrNull(statistic(statistics.statistics, "Blocked Shots")),
        integerOrNull(statistic(statistics.statistics, "Goalkeeper Saves")),
        integerOrNull(statistic(statistics.statistics, "Corner Kicks")),
        integerOrNull(statistic(statistics.statistics, "Fouls")),
        integerOrNull(statistic(statistics.statistics, "Yellow Cards")),
        integerOrNull(statistic(statistics.statistics, "Red Cards")),
        integerOrNull(statistic(statistics.statistics, "Total passes")),
        integerOrNull(statistic(statistics.statistics, "Passes accurate")),
        numberOrNull(statistic(statistics.statistics, "Passes %")),
        String(fixture.providerFixtureId),
        JSON.stringify(statistics.raw ?? statistics),
      ],
    );
  }

  async upsertPlayerStats(
    client,
    {
      fixture,
      matchId,
      playerId,
      teamId,
      opponentTeamId,
      started,
      player,
    },
  ) {
    const appearance = player.appearance ?? {};
    await client.query(
      `
        insert into public.player_match_stats (
          match_id,
          player_id,
          team_id,
          opponent_team_id,
          started,
          minutes_played,
          goals,
          assists,
          shots,
          shots_on_target,
          key_passes,
          tackles,
          interceptions,
          saves,
          yellow_cards,
          red_cards,
          source_name,
          source_player_key,
          captured_at,
          raw_payload
        )
        values (
          $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
          $11, $12, $13, $14, $15, $16, 'api_football', $17, now(), $18
        )
        on conflict (match_id, player_id, team_id)
        do update set
          opponent_team_id = excluded.opponent_team_id,
          started = excluded.started,
          minutes_played = excluded.minutes_played,
          goals = excluded.goals,
          assists = excluded.assists,
          shots = excluded.shots,
          shots_on_target = excluded.shots_on_target,
          key_passes = excluded.key_passes,
          tackles = excluded.tackles,
          interceptions = excluded.interceptions,
          saves = excluded.saves,
          yellow_cards = excluded.yellow_cards,
          red_cards = excluded.red_cards,
          captured_at = excluded.captured_at,
          raw_payload = excluded.raw_payload
      `,
      [
        matchId,
        playerId,
        teamId,
        opponentTeamId,
        started,
        integerOrNull(appearance.minutes),
        integerOrNull(appearance.goals),
        integerOrNull(appearance.assists),
        integerOrNull(appearance.shots),
        integerOrNull(appearance.shotsOnTarget),
        integerOrNull(appearance.keyPasses),
        integerOrNull(appearance.tackles),
        integerOrNull(appearance.interceptions),
        integerOrNull(appearance.saves),
        integerOrNull(appearance.yellowCards),
        integerOrNull(appearance.redCards),
        `api_football:${player.player.providerId}`,
        JSON.stringify({
          fixture_id: fixture.providerFixtureId,
          player: player.raw ?? player,
        }),
      ],
    );
  }

  async ingestFixture(
    { fixture, statistics, players, lineups },
    { client: externalClient } = {},
  ) {
    const client = externalClient ?? await this.pool.connect();
    const ownsTransaction = !externalClient;
    try {
      if (ownsTransaction) await client.query("begin");

      const homeTeamId = await this.upsertTeam(client, fixture.homeTeam);
      const awayTeamId = await this.upsertTeam(client, fixture.awayTeam);
      const matchId = await this.upsertMatch(
        client,
        fixture,
        homeTeamId,
        awayTeamId,
      );
      const teamIds = new Map([
        [fixture.homeTeam.providerId, homeTeamId],
        [fixture.awayTeam.providerId, awayTeamId],
      ]);

      for (const item of statistics) {
        const teamId = teamIds.get(item.team.providerId);
        if (!teamId) continue;
        const isHome = item.team.providerId === fixture.homeTeam.providerId;
        await this.upsertTeamStats(client, {
          fixture,
          matchId,
          teamId,
          opponentTeamId: isHome ? awayTeamId : homeTeamId,
          isHome,
          statistics: item,
        });
      }

      const starters = new Set(
        lineups.flatMap((lineup) =>
          lineup.starters.map((player) => player.providerId)
        ),
      );
      const lineupPlayers = new Map(
        lineups.flatMap((lineup) =>
          [...lineup.starters, ...lineup.substitutes].map((player) => [
            player.providerId,
            { ...player, team: lineup.team },
          ])
        ),
      );
      const appearances = new Map(
        players.map((player) => [player.player.providerId, player]),
      );

      for (const [providerId, lineupPlayer] of lineupPlayers) {
        if (!appearances.has(providerId)) {
          appearances.set(providerId, {
            team: lineupPlayer.team,
            player: {
              providerId,
              name: lineupPlayer.name,
              photo: null,
            },
            appearance: {
              minutes: null,
              position: lineupPlayer.position,
            },
            raw: { lineup_only: true },
          });
        }
      }

      for (const player of appearances.values()) {
        const teamId = teamIds.get(player.team.providerId);
        if (!teamId) continue;
        const isHome = player.team.providerId === fixture.homeTeam.providerId;
        const playerId = await this.upsertPlayer(
          client,
          player.player,
          teamId,
          player.appearance?.position,
        );
        await this.upsertPlayerStats(client, {
          fixture,
          matchId,
          playerId,
          teamId,
          opponentTeamId: isHome ? awayTeamId : homeTeamId,
          started: starters.has(player.player.providerId),
          player,
        });
      }

      if (ownsTransaction) await client.query("commit");
      return {
        matchId,
        teamStats: statistics.length,
        playerStats: appearances.size,
      };
    } catch (error) {
      if (ownsTransaction) await client.query("rollback");
      throw error;
    } finally {
      if (ownsTransaction) client.release();
    }
  }
}
