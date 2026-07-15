# local-agent-tracker — Architecture

A local, zero-daemon pipeline that tracks the **performance and cost** of two AI coding agents — **Claude Code** and **pi** — so their costs can be compared across LLMs and across harness changes, and optimized deliberately instead of guessed at.

This document is the outcome of a researched, interview-driven design session (2026-07-15). Terminology is defined in [CONTEXT.md](./CONTEXT.md); hard-to-reverse decisions are recorded in [docs/adr/](./docs/adr/).

---

## 1. Purpose & constraints

**Primary question the system must answer:** *"What does a unit of work cost on model A vs model B — and did my last harness change make the same model cheaper or more expensive?"*

Constraints that shaped every decision:

- **Host**: laptop `VPCCA15FB`, 7.7 GB RAM, earlyoom installed after a swap-thrash livelock. **No always-on daemons locally.**
- Both agents already persist complete token/usage data to local JSONL — collection can be entirely passive.
- Claude Code cleans up transcripts after ~30 days, so ingested history must outlive the source files.
- Rich visualization belongs to the existing homelab Grafana (kube-prometheus-stack); this app only delivers queryable data to it, plus a lightweight local view.

## 2. System shape

```
~/.claude/projects/**/<session>.jsonl ──┐
~/.pi/agent/sessions/**/<session>.jsonl ┼─▶ collector (systemd timer, every 5 min + on-boot, runs and exits)
~/.pi/agent/run-history.jsonl ──────────┘        │
                                     ┌───────────┴───────────┐
                                     ▼                       ▼
                           SQLite (local source        homelab Postgres
                           of truth, full history)     (idempotent upsert over Tailscale)
                               │         │                   ▼
                     CLI report   dashboard.html       Grafana (existing, native Postgres
                     (terminal)   (static, self-       datasource) — homelab dashboards are
                                  contained, re-       user-managed, outside this app
                                  rendered each run)
```

- **Language**: Python 3.12, mirroring `mcp-plane` conventions (venv, pytest, requirements files).
- **No live telemetry**: no Claude Code OTel, no pi extension, no OTLP receiver. See [ADR-0001](./docs/adr/0001-batch-jsonl-over-live-telemetry.md).
- **Two stores, one truth**: SQLite is canonical; Postgres is a shipped replica for Grafana. See [ADR-0002](./docs/adr/0002-sqlite-source-of-truth-postgres-for-grafana.md).

## 3. Data sources

### 3.1 pi sessions — `~/.pi/agent/sessions/<cwd-slug>/<iso-ts>_<uuid>.jsonl`

Session format v3: one JSON entry per line, forming a tree via `id`/`parentId`. Entry types: `session` (header: `cwd`, session id), `model_change`, `thinking_level_change`, `message`.

Usage lives on `message` entries with `message.role == "assistant"` (verified against live files 2026-07-15):

| Field | Meaning |
|---|---|
| `message.model`, `message.responseModel`, `message.provider`, `message.api` | model identity (requested vs actually served) |
| `message.usage.input / output / cacheRead / cacheWrite / totalTokens` | token counts (`cacheWrite1h` appears for Anthropic 1h cache) |
| `message.usage.cost.{input,output,cacheRead,cacheWrite,total}` | **cost in USD, precomputed by pi** — Reported Cost |
| `message.stopReason` | e.g. `toolUse`, `stop` |
| entry `timestamp` (ISO) + `message.timestamp` (epoch ms) | timing |

Tool outcomes: `role: "toolResult"` messages carry `toolName`, `toolCallId`, `isError`. Assistant `content` contains `toolCall` items.

**Absent from pi data**: per-message durations. Latency must be derived by diffing consecutive entry timestamps within a session tree.

Supplementary: `~/.pi/agent/run-history.jsonl` — one line per subagent run: `agent`, `task`, `ts` (epoch s), `status` (`ok`/`error`), `duration` (ms). No tokens/cost.

### 3.2 Claude Code transcripts — `~/.claude/projects/<cwd-slug>/<session-uuid>.jsonl`

Subagent transcripts live under `<session-uuid>/subagents/agent-*.jsonl` (with `.meta.json` siblings) and must be ingested too — subagent tokens are real spend.

Usage lives on lines with `type == "assistant"`:

| Field | Meaning |
|---|---|
| `message.model` | model id, e.g. `claude-fable-5` |
| `message.usage.input_tokens / output_tokens / cache_creation_input_tokens / cache_read_input_tokens` | token counts |
| `message.usage.cache_creation.ephemeral_1h_input_tokens / ephemeral_5m_input_tokens` | cache-write split — **priced differently, must not be merged** |
| `message.usage.iterations[]` | one entry per underlying API call in the turn (retries); top-level usage is the billed total |
| `message.stop_reason`, `diagnostics.cache_miss_reason` | reliability / cache-behavior signals |
| `timestamp` (ISO), `sessionId`, `cwd`, `gitBranch`, `version`, `requestId`, `uuid`/`parentUuid` | context + conversation tree |
| `tool_use` / `tool_result` content blocks (`name`, `is_error`) | tool activity |

**Absent from Claude Code data**: any cost field. Verified by grep across multiple transcripts — cost must be **derived** (§7). Also absent: reliable productivity metrics (lines of code, commits), which is why they are out of scope.

Non-sources, checked and rejected: `~/.claude/history.jsonl` (prompt text history only), `~/.claude/telemetry/1p_failed_events*.json` (failed-export queue; successful events never persist locally).

## 4. Components

All components live in this repo and run from one entry point: `tracker <command>`.

### 4.1 Collector (`tracker collect`)
- Fired by a **systemd user timer every 5 minutes** plus `OnBootSec`; runs, ingests, renders, ships, exits. `flock` guard against overlapping runs.
- **Incremental** via per-file watermarks: an `ingest_files` table stores `(path, inode, byte_offset, mtime)`; only new bytes are parsed. Rotated/truncated files (inode or shrunken size mismatch) are re-ingested from zero — safe because all writes are idempotent upserts keyed on natural ids (`sessionId` + message `uuid`/`requestId` for Claude Code; session file + entry `id` for pi).
- Parsing is tolerant: unknown entry types and malformed lines are counted and skipped, never fatal (agents update; schemas drift).

### 4.2 SQLite store (`~/.local/share/local-agent-tracker/tracker.db`)
Canonical, append-mostly schema (sketch — final DDL in the implementation):

- `messages` — one row per assistant message: agent (`claude-code` | `pi`), session id, project/cwd, timestamp, model, provider, token columns (normalized names), reported_cost, derived_cost, pricing_version, stop_reason, retry_count, latency_ms (derived), context_tokens.
- `sessions` — one row per session: agent, cwd, git branch, start/end, message counts, rollup cost/tokens.
- `tool_calls` — tool name, agent, session, is_error, timestamp (durations where derivable).
- `subagent_runs` — from pi `run-history.jsonl` and Claude Code subagent transcripts.
- `fingerprints` — time-ranged mapping of config-repo HEADs (§6).
- `experiments` — named user-declared windows (§6).
- `pricing` — the vendored rate table, versioned (§7).
- `ingest_files` — watermarks (§4.1).

Retention: keep everything. Personal-scale data (thousands of rows/month) is negligible next to the value of long-baseline comparisons.

### 4.3 Shipper (embedded in `tracker collect`, also `tracker ship`)
- Upserts `messages`, `sessions`, `tool_calls`, `subagent_runs`, `experiments`, `fingerprints` into a small **homelab Postgres** over Tailscale (`ON CONFLICT ... DO UPDATE`, natural keys — replays are harmless).
- A `shipped_at` column in SQLite marks delivery; **homelab unreachable ⇒ rows simply wait for the next run**. The laptop never blocks on the homelab.
- Credentials come from the environment/systemd unit, never from the repo.
- Grafana reads Postgres via its native datasource; **dashboards there are user-managed and out of this app's scope.**

### 4.4 Static dashboard renderer (embedded in `tracker collect`, also `tracker render`)
- Each run re-renders `~/.local/share/local-agent-tracker/dashboard.html`: a **self-contained** file (inline JS/CSS, embedded data, no server, no CDN) opened directly in a browser.
- Core views, matching the primary question: cost by model over time; cost per session/day; same-model comparison across harness fingerprints and experiment windows; cache-hit rate; retry and tool-error rates.

### 4.5 CLI reports
- `tracker report --daily | --sessions | --models | --experiment <name>` — terminal tables straight from SQLite.
- `tracker experiment start|stop "<name>"` — manage experiment windows.
- `tracker pricing update` — refresh the vendored rate table (§7).

## 5. Metric catalog

| Family | Metrics | Derivation |
|---|---|---|
| **Cost & tokens** | input/output/cacheRead/cacheWrite tokens (Claude 5m vs 1h cache-write kept separate), cost USD — sliced by model, provider, agent, project, session, fingerprint, experiment | pi: Reported Cost from `usage.cost`; Claude Code: Derived Cost = tokens × pricing table |
| **Performance** | turn latency, tokens/turn, retries per turn, cache-hit rate, context-window growth, subagent durations | latency = timestamp diffs (both agents); retries = `len(usage.iterations) - 1`; cache-hit = cacheRead ÷ (cacheRead + input + cacheWrite); subagent durations from `run-history.jsonl` / `.meta.json` |
| **Reliability** | tool-error rate per tool name, stop-reason distribution, retry/api-error incidence | `is_error` on tool results; `stop_reason`; `iterations[]` |
| **Usage patterns** | sessions per day/project, prompts per session, tool-usage mix, model-switch frequency | row counts + `model_change` entries |

Reliability lives here deliberately: error→retry loops are a *cost* phenomenon — they explain anomalies the cost family alone can't.

## 6. Harness attribution — the key dimension

Nothing in either JSONL says *which version of the harness produced a session*, yet "same model, before vs after my change" is the core query. Two complementary mechanisms ([ADR-0003](./docs/adr/0003-harness-fingerprint-plus-experiment-windows.md)):

1. **Harness Fingerprint (automatic, retroactive).** The collector walks the git history of the config repos — `pi-config`, `ai-skills`, `claude-config` — and materializes, per repo, time ranges of "commit X was HEAD from T1 to T2" into `fingerprints`. Every message joins to the fingerprint active at its timestamp. Works for all past sessions with zero user discipline.
2. **Experiment (explicit, intentional).** `tracker experiment start "kimi-cache-tuning"` … `stop` records a named window in `experiments`. Messages join by time range. This captures *why* a change was made, which commit hashes cannot.

Grafana/SQL comparisons then reduce to `GROUP BY model, fingerprint` or `GROUP BY model, experiment`.

## 7. Pricing strategy (Claude Code cost derivation)

([ADR-0004](./docs/adr/0004-vendored-pricing-with-stored-rates.md))

- A pricing JSON is **vendored in this repo**, seeded from LiteLLM's community-maintained `model_prices_and_context_window.json`, covering input/output/cache-read/cache-write rates — including the ephemeral **5m vs 1h cache-write price split**.
- Refreshed only by an explicit `tracker pricing update` (fetch → diff → commit); collector runs are deterministic and offline-safe.
- Every `messages` row stores the **pricing version used** — history is never silently repriced when rates change.
- **Unknown models are flagged (`derived_cost = NULL`, warning in the run log), never guessed.**
- Validation: cost math is cross-checked against `ccusage` (Claude Code) and `@ccusage/pi` output for the same day before trusting dashboards.

## 8. Operational notes

- **systemd user units**: `agent-tracker.timer` (`OnCalendar=*:0/5`, `OnBootSec=2min`, `Persistent=true`) → `agent-tracker.service` (`Type=oneshot`). No root, no Docker.
- **Idempotency everywhere**: re-running the collector over already-seen data must be a no-op. This is the invariant that makes crash recovery, re-ingestion, and Postgres replays all trivial.
- **Failure modes**: homelab down → ship later (§4.3); malformed JSONL line → skip and count; source file rotated → re-ingest, upserts dedupe; pricing missing a model → NULL cost + warning, never fabricate.
- **Security**: the collector reads agent transcripts, which can contain sensitive content. Only usage/metadata fields are extracted — **message content is never stored or shipped**. Postgres credentials live in the systemd unit environment.
- **Resource budget**: steady-state RAM ≈ 0 (timer process exits); a collect run is a short single-process Python burst; SQLite + rendered HTML are a few MB.

## 9. Rejected alternatives

| Alternative | Why rejected |
|---|---|
| Claude Code OTel (+ OTLP collector) & live pi extension | Needs an always-on receiver — violates the zero-daemon constraint; adds no data the JSONL lacks. pi's `message_end` extension hook (full `usage.cost` per message) is documented as the upgrade path if sub-minute liveness is ever needed. |
| Native VictoriaMetrics + Grafana Alloy + Grafana (~250–500 MB) | Lightest "real TSDB" option, but still permanent RAM on a memory-pressured laptop, and PromQL fits the relational A/B queries poorly. |
| docker-compose OTel stacks (claude-code-otel, claude-code-metrics-stack, LGTM) | Heaviest option (4 containers + dockerd); sized for teams, not one laptop. Kept only as dashboard-JSON inspiration. |
| Prometheus pushgateway into kube-prometheus-stack | Last-value gauge semantics — no history, no backfill. Wrong shape for batch-shipped events. |
| Syncing the SQLite file to the homelab (rsync/litestream) + Grafana SQLite plugin | Community-grade plugin, file-locking hazards during sync; native Postgres datasource is strictly more robust. |
| Homelab-only (no local store) | Makes history hostage to homelab availability; SQLite is also what outlives Claude Code's 30-day transcript cleanup. |

## 10. Ecosystem references

- [ryoppippi/ccusage](https://github.com/ryoppippi/ccusage) + [`@ccusage/pi`](https://www.npmjs.com/package/@ccusage/pi) — zero-daemon JSONL cost CLIs; used to validate our cost math.
- [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor) — live TUI + local usage warehouse; prior art for "persist beyond transcript cleanup".
- [Claude Code monitoring docs](https://code.claude.com/docs/en/monitoring-usage) — first-party OTel surface (the rejected live path).
- [mprokopov/pi-otel-telemetry](https://github.com/mprokopov/pi-otel-telemetry) — community pi OTel extension (rejected live path; single-maintainer).
- [acreeger/claude-code-metrics-stack](https://github.com/acreeger/claude-code-metrics-stack), [ColeMurray/claude-code-otel](https://github.com/ColeMurray/claude-code-otel) — dashboard-design references only.
