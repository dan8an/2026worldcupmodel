from ..features.context import (
    AVAILABILITY_REPORTS,
    RAW_RESULTS,
    SQUAD_SELECTIONS,
    load_availability_reports,
    load_historical_results,
    load_squad_selections,
)


if __name__ == "__main__":
    results = load_historical_results()
    reports = load_availability_reports()
    squads = load_squad_selections()
    print(f"{RAW_RESULTS}: {len(results)} mapped historical results")
    print(f"{AVAILABILITY_REPORTS}: {len(reports)} active-format reports")
    print(f"{SQUAD_SELECTIONS}: {len(squads)} sourced squad selections")
