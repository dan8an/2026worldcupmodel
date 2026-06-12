-- Add the missing men's New Zealand team mapping used by prediction generation.
-- API-Football team 4673 is New Zealand; 1716 is the separate women's team.

update public.teams
set api_football_team_id = 4673
where lower(name) = 'new zealand'
  and api_football_team_id is null;

insert into public.teams (
  name,
  fifa_rank,
  elo_rating,
  confederation,
  api_football_team_id
)
select
  'New Zealand',
  86,
  1446,
  'OFC',
  4673
where not exists (
  select 1
  from public.teams
  where lower(name) = 'new zealand'
     or api_football_team_id = 4673
);
