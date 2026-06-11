import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import {
  ErrorState,
  Loading,
  percent,
  precisePercent,
  ProbabilityBar,
} from "../components";
import {
  additionalFactors,
  confidenceExplanation,
  confidenceLevel,
  eloBaseProbabilities,
  finalProbabilities,
  primaryFactors,
  predictionSummary,
} from "../prediction-display";

export function MatchDetail() {
  const { id = "" } = useParams();
  const query = useQuery({ queryKey: ["match", id], queryFn: () => api.match(id) });
  if (query.isLoading) return <Loading label="Loading match" />;
  if (query.isError || !query.data) return <ErrorState />;
  const match = query.data;
  const prediction = match.prediction;
  if (!prediction) return <ErrorState />;
  const final = finalProbabilities(prediction);
  const eloBase = eloBaseProbabilities(prediction);
  const confidence = confidenceLevel(prediction.confidence_score);
  const confidenceCopy = confidenceExplanation(prediction.confidence_score);
  const factors = primaryFactors(prediction);
  const additional = additionalFactors(prediction);
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
          <span className={`confidence-pill ${confidence.toLowerCase().replace(" ", "-")}`}>
            {confidence} confidence
            {prediction.confidence_score != null &&
              ` · ${Math.round(prediction.confidence_score)}/100`}
          </span>
          <span>Model: {prediction.model_version}</span>
        </div>
      </div>
      <div className="outcome-grid" aria-label="Final result probabilities">
        {[
          ["Win", match.home_team?.name, final.home, "home"],
          ["Draw", "Stalemate", final.draw, "draw"],
          ["Loss", match.away_team?.name, final.away, "away"],
        ].map(([label, team, probability, className]) => (
          <article className={`outcome-card ${className}`} key={label}>
            <span>{label}</span>
            <strong>{precisePercent(probability as number)}</strong>
            <small>{team}</small>
          </article>
        ))}
      </div>
      <div className="detail-grid">
        <article className="panel explanation-panel">
          <span className="eyebrow">Model interpretation</span>
          <h2>How the forecast moved</h2>
          <p className="explanation-summary">{predictionSummary(match)}</p>
          <p className="confidence-explanation">
            <strong>{confidence} confidence:</strong> {confidenceCopy}
          </p>
          <div className="probability-comparison">
            <div className="comparison-heading">
              <span>Outcome</span>
              <span>Elo base</span>
              <span>Final</span>
              <span>Change</span>
            </div>
            {[
              ["Win", eloBase?.home, final.home],
              ["Draw", eloBase?.draw, final.draw],
              ["Loss", eloBase?.away, final.away],
            ].map(([label, base, finalValue]) => {
              const change =
                typeof base === "number" ? (finalValue as number) - base : null;
              return (
                <div className="comparison-row" key={label}>
                  <strong>{label}</strong>
                  <span>{typeof base === "number" ? precisePercent(base) : "—"}</span>
                  <span>{precisePercent(finalValue as number)}</span>
                  <b className={change == null ? "" : change >= 0 ? "positive" : "negative"}>
                    {change == null ? "—" : `${change >= 0 ? "↑" : "↓"} ${precisePercent(Math.abs(change))}`}
                  </b>
                </div>
              );
            })}
          </div>
        </article>
        <article className="panel factor-panel">
          <span className="eyebrow">Primary factors</span>
          <h2>Strongest model signals</h2>
          {factors.length ? (
            <ol className="factor-list">
              {factors.map((factor) => (
                <li key={`${factor.factor}-${factor.team}`}>
                  <div>
                    <strong>{factor.factor}</strong>
                    <span>{factor.team}</span>
                  </div>
                  <b className={`factor-impact ${factor.direction}`}>
                    {factor.direction === "negative" ? "↓" : "↑"} {factor.impact}
                  </b>
                </li>
              ))}
            </ol>
          ) : (
            <ol className="factor-list legacy">
              {prediction.key_factors.slice(0, 3).map((factor) => (
                <li key={factor}>{factor}</li>
              ))}
            </ol>
          )}
          {additional.length > 0 && (
            <div className="additional-factors">
              <h3>Additional context</h3>
              <ul>
                {additional.map((factor) => (
                  <li key={`${factor.factor}-${factor.team}`}>
                    <span>{factor.factor} · {factor.team}</span>
                    <b className={factor.direction}>{factor.impact}</b>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </article>
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
        <article className="panel methodology-panel">
          <span className="eyebrow">Structured explanation</span>
          <h2>Data and model context</h2>
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
