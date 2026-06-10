import math

from .domain import MatchContext, MatchPrediction, ScoreProbability, Team

NEUTRAL_TEAM_XG = 1.2
HOST_ELO_ADVANTAGE = 50.0


def poisson_probability(goals: int, expected_goals: float) -> float:
    return math.exp(-expected_goals) * expected_goals**goals / math.factorial(goals)


def expected_goals(
    home: Team,
    away: Team,
    context: MatchContext | None = None,
) -> tuple[float, float]:
    """Return neutral-site xG with an adjustment only for tournament co-hosts.

    "Home" and "away" describe fixture ordering. They do not create a sporting
    advantage at this tournament. Mexico, Canada, and the United States receive
    the host adjustment through their seed-data `host` flag, regardless of which
    side of the fixture they occupy.
    """
    context = context or MatchContext()
    elo_gap = home.elo - away.elo
    adjusted_gap = (
        elo_gap
        + (HOST_ELO_ADVANTAGE if home.host else 0.0)
        - (HOST_ELO_ADVANTAGE if away.host else 0.0)
        + context.home_adjustment
        - context.away_adjustment
    )
    home_xg = NEUTRAL_TEAM_XG * math.exp(adjusted_gap / 800)
    away_xg = NEUTRAL_TEAM_XG * math.exp(-adjusted_gap / 800)
    return min(max(home_xg, 0.25), 3.8), min(max(away_xg, 0.25), 3.8)


def _confidence(probabilities: tuple[float, float, float]) -> str:
    entropy = -sum(p * math.log(p) for p in probabilities if p > 0) / math.log(3)
    if entropy < 0.72:
        return "Higher confidence"
    if entropy < 0.9:
        return "Moderate confidence"
    return "High uncertainty"


def predict_match(
    home: Team,
    away: Team,
    match_id: str,
    max_goals: int = 8,
    context: MatchContext | None = None,
) -> MatchPrediction:
    context = context or MatchContext()
    home_xg, away_xg = expected_goals(home, away, context)
    scores: list[ScoreProbability] = []
    home_win = draw = away_win = 0.0
    for home_goals in range(max_goals + 1):
        for away_goals in range(max_goals + 1):
            probability = (
                poisson_probability(home_goals, home_xg)
                * poisson_probability(away_goals, away_xg)
            )
            scores.append(ScoreProbability(home_goals, away_goals, probability))
            if home_goals > away_goals:
                home_win += probability
            elif home_goals == away_goals:
                draw += probability
            else:
                away_win += probability
    total = home_win + draw + away_win
    probabilities = (home_win / total, draw / total, away_win / total)
    top_scores = tuple(
        ScoreProbability(score.home, score.away, score.probability / total)
        for score in sorted(scores, key=lambda score: score.probability, reverse=True)[:5]
    )
    gap = round(
        home.elo
        - away.elo
        + context.home_adjustment
        - context.away_adjustment
    )
    favorite = home.name if gap >= 0 else away.name
    venue_factor = (
        f"{home.name} receives the tournament co-host adjustment"
        if home.host
        else f"{away.name} receives the tournament co-host adjustment"
        if away.host
        else "Fixture ordering adds no home advantage; this is modeled as a neutral-site match"
    )
    factors: list[str] = [
        f"{favorite} has the stronger baseline rating",
        f"Adjusted rating gap: {abs(gap)} Elo-equivalent points",
        venue_factor,
    ]
    if context.historical_matches_home or context.historical_matches_away:
        form_gap = context.home_form_elo - context.away_form_elo
        form_team = home.name if form_gap >= 0 else away.name
        factors.append(
            f"Recent weighted form favors {form_team} "
            f"({context.historical_matches_home}/{context.historical_matches_away} matches)"
        )
    else:
        factors.append("Recent-results feed is not available; no form adjustment is applied")
    if context.h2h_matches:
        factors.append(
            f"Head-to-head adjustment uses {context.h2h_matches} recent meetings and is capped"
        )
    if context.availability_reports:
        factors.append(
            f"{context.availability_reports} sourced player availability reports affect this forecast"
        )
    return MatchPrediction(
        match_id=match_id,
        home_team_id=home.id,
        away_team_id=away.id,
        home_xg=round(home_xg, 3),
        away_xg=round(away_xg, 3),
        home_win=round(probabilities[0], 6),
        draw=round(probabilities[1], 6),
        away_win=round(probabilities[2], 6),
        top_scores=top_scores,
        confidence=_confidence(probabilities),
        key_factors=tuple(factors),
        context=context,
    )
