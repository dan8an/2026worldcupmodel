import { ApiFootballProvider } from "./api-football.js";
import { SampleSportsProvider } from "./sample-provider.js";

export const createSportsProvider = (
  env = process.env,
  { fetchImpl = globalThis.fetch, logger = console } = {},
) => {
  const providerName = env.SPORTS_PROVIDER || "api_football";

  if (providerName !== "api_football") {
    throw new Error(`Unsupported SPORTS_PROVIDER: ${providerName}`);
  }

  if (!env.API_FOOTBALL_KEY) {
    logger.warn(
      "[sports] API_FOOTBALL_KEY is missing; using local sample data",
    );
    return new SampleSportsProvider({ logger });
  }

  logger.info("[sports] Using API-Football provider");
  return new ApiFootballProvider({
    apiKey: env.API_FOOTBALL_KEY,
    baseUrl: env.API_FOOTBALL_BASE_URL,
    fetchImpl,
    logger,
  });
};
