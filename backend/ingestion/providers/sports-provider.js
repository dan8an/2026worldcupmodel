export class SportsProvider {
  constructor(name) {
    this.name = name;
  }

  async get_completed_matches(_date) {
    throw new Error(`${this.name} does not implement get_completed_matches`);
  }

  async get_fixture_statistics(_fixtureId) {
    throw new Error(`${this.name} does not implement get_fixture_statistics`);
  }

  async get_fixture_players(_fixtureId) {
    throw new Error(`${this.name} does not implement get_fixture_players`);
  }

  async get_lineups(_fixtureId) {
    throw new Error(`${this.name} does not implement get_lineups`);
  }
}
