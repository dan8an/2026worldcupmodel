import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { ErrorState, Loading, percent, ProbabilityBar } from "../components";

export function MatchDetail() {
  const { id = "" } = useParams();
  const query = useQuery({ queryKey: ["match", id], queryFn: () => api.match(id) });
  if (query.isLoading) return <Loading label="Loading match" />;
  if (query.isError || !query.data) return <ErrorState />;
  const match = query.data;
  const prediction = match.prediction;
  if (!prediction) return <ErrorState />;
  return (
    <section>
      <div className="match-hero">
        <div className="eyebrow">Group {match.group} · Match {match.number}</div>
        <div className="versus">
          <Link to={`/teams/${match.home_team?.id}`}>
            <span className="flag match-flag" aria-hidden="true">{match.home_team?.flag}</span>
            <span>{match.home_team?.name}</span>
          </Link>
          <div>
            <span>Expected goals</span>
            <strong>{prediction.home_xg.toFixed(2)} : {prediction.away_xg.toFixed(2)}</strong>
          </div>
          <Link to={`/teams/${match.away_team?.id}`}>
            <span className="flag match-flag" aria-hidden="true">{match.away_team?.flag}</span>
            <span>{match.away_team?.name}</span>
          </Link>
        </div>
        <ProbabilityBar match={match} />
        <div className="match-meta">
          <span>{new Date(match.kickoff).toLocaleString()}</span>
          <span>{prediction.confidence}</span>
          <span>{prediction.model_version}</span>
        </div>
      </div>
      <div className="detail-grid">
        <article className="panel">
          <span className="eyebrow">Score distribution</span>
          <h2>Most likely scorelines</h2>
          {prediction.top_scores.map((score) => (
            <div className="score-row" key={`${score.home}-${score.away}`}>
              <strong>{score.home} - {score.away}</strong>
              <div className="mini-track"><span style={{ width: `${score.probability * 500}%` }} /></div>
              <b>{percent(score.probability)}</b>
            </div>
          ))}
        </article>
        <article className="panel">
          <span className="eyebrow">Structured explanation</span>
          <h2>What drives the forecast</h2>
          <ol className="factor-list">
            {prediction.key_factors.map((factor) => <li key={factor}>{factor}</li>)}
          </ol>
          <p className="disclosure">
            Generated from model inputs, not news inference. Data cutoff:{" "}
            {new Date(prediction.data_cutoff).toLocaleString()}.
          </p>
          <p className="disclosure">
            Coverage: {prediction.context.historical_matches_home} recent{" "}
            {match.home_team?.name} matches,{" "}
            {prediction.context.historical_matches_away} recent{" "}
            {match.away_team?.name} matches, {prediction.context.h2h_matches}{" "}
            head-to-head meetings, and {prediction.context.availability_reports}{" "}
            active sourced availability reports.
          </p>
        </article>
      </div>
    </section>
  );
}
