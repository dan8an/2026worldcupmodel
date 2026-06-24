import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api } from "../api";
import { ErrorState, Loading, MatchCard } from "../components";
import { matchSchedule } from "../match-status";

export function Matches() {
  const query = useQuery({ queryKey: ["matches"], queryFn: api.matches });
  const [group, setGroup] = useState("ALL");
  const [search, setSearch] = useState("");
  const schedule = useMemo(() => matchSchedule(query.data ?? []), [query.data]);
  const filtered = useMemo(
    () =>
      schedule.upcoming.filter((match) => {
        const names = `${match.home_team?.name} ${match.away_team?.name}`.toLowerCase();
        return (group === "ALL" || match.group === group) && names.includes(search.toLowerCase());
      }),
    [group, schedule.upcoming, search],
  );
  if (query.isLoading) return <Loading label="Loading matches" />;
  if (query.isError) return <ErrorState />;
  return (
    <section>
      <div className="page-heading">
        <span className="eyebrow">Upcoming group fixtures</span>
        <h1>Match forecasts</h1>
        <p>Filter by group or team. Probabilities are frozen by model snapshot.</p>
      </div>
      <div className="filters">
        <input
          aria-label="Search teams"
          placeholder="Search a team"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <select value={group} onChange={(event) => setGroup(event.target.value)}>
          <option value="ALL">All groups</option>
          {"ABCDEFGHIJKL".split("").map((letter) => (
            <option key={letter} value={letter}>Group {letter}</option>
          ))}
        </select>
      </div>
      <div className="card-grid">
        {filtered.map((match) => (
          <MatchCard
            key={match.id}
            match={match}
            displayNumber={schedule.numberById.get(match.id)}
          />
        ))}
      </div>
      {filtered.length === 0 && (
        <div className="state-card empty-state">
          No upcoming matches match these filters. Completed and in-progress fixtures are listed in Results.
        </div>
      )}
    </section>
  );
}
