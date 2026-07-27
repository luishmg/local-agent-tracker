# local-agent-tracker

Tracks performance and cost of local AI coding agents (Claude Code, pi) to enable cost optimization across models and harness configurations.

## Language

**Agent**:
One of the tracked AI coding tools — Claude Code or pi. The value of the `agent` dimension on every stored row.
_Avoid_: tool, CLI, assistant

**Session**:
One conversation with an Agent, identified by the agent's own session id and persisted by it as a single JSONL transcript.
_Avoid_: run, conversation, chat

**Message**:
One assistant response inside a Session — the unit that carries token usage and cost. The finest grain stored. Claude Code writes a single Message as several JSONL lines, one per content block, each repeating the same usage; the `message.id` those lines share — not the per-line `uuid` — is the Message's identity.
_Avoid_: turn (a turn may span multiple API calls), completion, response, line

**Dedup Key**:
The globally scoped identity a stored row is keyed on, making ingestion idempotent. For a Message it is the provider's `message.id` (Claude Code) or a hash of the entry's stable fields (pi, which assigns none). Global, never per-Session, because both Agents copy Message history into a new transcript when a Session is resumed.
_Avoid_: natural key, primary key, uuid

**Collector Run**:
One timer-fired execution of the collector: ingest → derive → render → ship → exit.
_Avoid_: scrape, poll, sync

**Watermark**:
The per-source-file byte offset (with inode/mtime) up to which ingestion is complete; makes Collector Runs incremental.
_Avoid_: offset, cursor, checkpoint

**Harness**:
The user-controlled configuration surrounding an Agent — the pi-config, ai-skills, and claude-config repos — whose changes alter agent behavior and cost.
_Avoid_: setup, config, environment

**Harness Fingerprint**:
The automatically derived identity of the Harness at a point in time: which commit was HEAD in each config repo. Retroactive, computed from git history.
_Avoid_: config version, snapshot

**Experiment**:
A user-declared, named time window (`tracker experiment start/stop`) marking an intentional Harness change under evaluation.
_Avoid_: test, trial, A/B window

**Reported Cost**:
Cost in USD as written by the Agent itself into its transcript (pi only).
_Avoid_: actual cost, real cost

**Derived Cost**:
Cost computed by this system from token counts × the Pricing Table (required for Claude Code, which reports no cost).
_Avoid_: estimated cost, calculated cost

**Pricing Table**:
The vendored, versioned JSON of per-model token rates used to compute Derived Cost; changed only by explicit update.
_Avoid_: price list, rates file

**Sink**:
The homelab Postgres that shipped rows are upserted into for Grafana to query. Downstream, optional, never load-bearing.
_Avoid_: remote, upstream, warehouse
