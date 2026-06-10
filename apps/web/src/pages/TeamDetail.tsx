import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { ErrorState, Loading, MatchCard, percent } from "../components";

const positionName: Record<string, string> = {
  GK: "Goalkeeper",
  DF: "Defender",
  MF: "Midfielder",
  FW: "Forward",
};

export function TeamDetail() {
  const { id = "" } = useParams();
  const query = useQuery({ queryKey: ["team", id], queryFn: () => api.team(id) });
  if (query.isLoading) return <Loading label="Loading team intelligence" />;
  if (query.isError || !query.data) return <ErrorState />;
  const team = query.data;
  const source = team.player_data_source;

  return (
    <section>
      <div className="team-hero profile-hero">
        <div>
          <span className="eyebrow">Group {team.group} intelligence report</span>
          <h1 className="team-title">
            <span className="flag profile-flag" aria-hidden="true">{team.flag}</span>
            {team.name}
          </h1>
          <p>
            Rank-derived strength, recent international form, squad experience,
            and simulated tournament paths.
          </p>
        </div>
        <div className="stat"><span>Round of 32</span><strong>{percent(team.tournament_probability.round_of_32)}</strong></div>
        <div className="stat"><span>Champion</span><strong>{percent(team.tournament_probability.champion)}</strong></div>
      </div>

      <div className="profile-metrics">
        <article><span>Recent record</span><strong>{team.form_summary.wins}-{team.form_summary.draws}-{team.form_summary.losses}</strong><small>Last {team.form_summary.matches} mapped matches</small></article>
        <article><span>Goals</span><strong>{team.form_summary.goals_for}:{team.form_summary.goals_against}</strong><small>For and against</small></article>
        <article><span>Points per match</span><strong>{team.form_summary.points_per_match.toFixed(2)}</strong><small>Displayed recent sample</small></article>
        <article><span>Baseline rank</span><strong>#{team.rank}</strong><small>Elo-equivalent {team.elo.toFixed(0)}</small></article>
      </div>

      <div className="profile-layout">
        <div className="profile-main">
          <article className="analysis-panel">
            <span className="eyebrow">Grounded AI analysis</span>
            <h2>{team.analysis.headline}</h2>
            <p className="lead-copy">{team.analysis.overview}</p>
            <h3>Recent form</h3>
            <p>{team.analysis.form}</p>
            <h3>Path through the group</h3>
            <p>{team.analysis.path}</p>
            <h3>Personnel outlook</h3>
            <p>{team.analysis.personnel}</p>
            <p className="disclosure">{team.analysis.method}</p>
          </article>

          <div className="section-heading compact">
            <div><span className="eyebrow">Model input</span><h2>Recent results</h2></div>
          </div>
          <div className="results-list">
            {team.recent_results.map((result) => (
              <div className="result-row" key={`${result.played_on}-${result.opponent_id}`}>
                <span className={`result-badge ${result.outcome.toLowerCase()}`}>{result.outcome}</span>
                <time>{new Date(`${result.played_on}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}</time>
                <Link to={`/teams/${result.opponent_id}`}>{result.opponent_name}</Link>
                <strong>{result.goals_for} - {result.goals_against}</strong>
                <small>{result.tournament}</small>
              </div>
            ))}
          </div>
        </div>

        <aside className="profile-side">
          <article className="panel">
            <span className="eyebrow">Tournament goals</span>
            <h2>Stage outlook</h2>
            {team.analysis.objectives.map((objective) => (
              <div className="objective-row" key={objective}>{objective}</div>
            ))}
          </article>
          <article className="panel">
            <span className="eyebrow">Group path</span>
            <h2>Match chances</h2>
            {team.group_path.map((match) => (
              <Link className="path-row" to={`/match/${match.match_id}`} key={match.match_id}>
                <span>vs {match.opponent_name}</span>
                <strong>{percent(match.team_win_probability)}</strong>
                <small>{new Date(match.kickoff).toLocaleDateString(undefined, { month: "short", day: "numeric" })}</small>
              </Link>
            ))}
          </article>
        </aside>
      </div>

      <section className="inner-section">
        <div className="section-heading">
          <div><span className="eyebrow">Squad snapshot</span><h2>Key players</h2></div>
          {source.source_url && <a href={source.source_url} target="_blank" rel="noreferrer">View squad source</a>}
        </div>
        <div className="player-grid">
          {team.key_players.map((player) => (
            <article className="player-card" key={player.name}>
              <div className="player-position">{player.position}</div>
              <h3>{player.name}</h3>
              <p>
                {positionName[player.position] ?? player.position} · {player.club}
                {player.age ? ` · Age ${player.age}` : ""}
              </p>
              <div className="player-numbers">
                <span><strong>{player.caps}</strong><small>Caps</small></span>
                <span><strong>{player.goals}</strong><small>Goals</small></span>
              </div>
              <small>{player.why_key}</small>
            </article>
          ))}
        </div>
        <p className="disclosure">
          {source.source_name}. Snapshot retrieved{" "}
          {source.retrieved_at ? new Date(source.retrieved_at).toLocaleString() : "date unavailable"}.
          Key-player ranking balances established international output with one
          younger high-upside squad member; it is deterministic, not an unsourced editorial judgment.
        </p>
      </section>

      <section className="inner-section">
        <div className="section-heading">
          <div><span className="eyebrow">2026 schedule</span><h2>Group matches</h2></div>
        </div>
        <div className="card-grid">
          {team.matches.map((match) => <MatchCard match={match} key={match.id} />)}
        </div>
      </section>
    </section>
  );
}
