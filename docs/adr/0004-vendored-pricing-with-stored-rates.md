# Vendored pricing table with per-row stored rates

Claude Code transcripts contain token counts but no cost, so cost is derived from a pricing JSON vendored in this repo (seeded from LiteLLM's community `model_prices` file, including the 5m vs 1h ephemeral cache-write split) and refreshed only by an explicit `tracker pricing update`. Every message row stores the pricing version used, so past costs are never silently repriced when rates change; unknown models get NULL cost and a warning, never a guess.

## Considered Options

- Fetch prices at runtime: rejected — network-dependent collector runs and retroactively shifting historical costs.
- Hand-maintained table: rejected — goes stale the moment a new model ships.

## Consequences

- Costs for brand-new models are missing until `tracker pricing update` is run — visible as flagged rows, which is the intended prompt to update.
- Derived cost is an approximation of billing; it is cross-validated against `ccusage` / `@ccusage/pi` output.
