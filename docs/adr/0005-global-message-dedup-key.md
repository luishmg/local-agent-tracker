# The dedup key is the provider message id, scoped globally — not the transcript line id, not the session

One assistant Message is identified by the id the provider assigned it (`message.id` for Claude Code; a composite hash for pi), and that identity is unique across the whole store rather than within a Session. Every ingested row is keyed this way, so re-ingesting a file, re-reading a rotated file, or ingesting two transcripts that both contain the same Message all converge on one row.

This supersedes the natural key described in earlier drafts of `architecture.md` §4.1 ("`sessionId` + message `uuid`/`requestId`"), which was written before the on-disk formats were measured. Both halves of it are wrong, and each one inflates cost independently.

**Claude Code writes one API response as several JSONL lines — one per content block — and repeats the complete `message.usage` on every one of them.** Measured on `~/.claude/projects/-home-luishmg-Projects/2611a5dd-….jsonl`: 755 lines with `type == "assistant"` carry only 368 distinct `message.id`, a 2.05× fan-out. A single response `msg_01D28axiw8xvj4qXz9cM9ST6` occupies seven lines — one `thinking`, one `text`, five `tool_use` — each with seven distinct line `uuid`s and each reporting the same `input_tokens: 4054, output_tokens: 3452`. Keying on the line `uuid` therefore bills one response up to seven times. `requestId` is shared across the blocks of a response but is not present on every line, so it cannot stand alone as the key either.

**Both agents copy Message history into new transcripts when a Session is resumed or forked.** Across a 200-file sample, 22,214 assistant lines resolved to 9,186 distinct `message.id`, of which 148 appear under more than one `sessionId`. pi does the same at entry granularity: the same entry `id`, `timestamp`, and `usage.totalTokens` appear in two Session files. A session-scoped key cannot see these, so every resumed Session is billed twice.

The two failures compound: a resumed, tool-heavy Session could be counted more than ten times over. Since the system exists to compare cost between models and across Harness changes, a multiplier that varies with tool density and resume frequency does not merely inflate the numbers — it destroys the comparison the numbers are for.

## Considered Options

- **Line `uuid`, session-scoped** (the earlier draft): rejected — over-counts by the content-block fan-out and again by resume-copying, as measured above.
- **`requestId`, session-scoped**: rejected — correct grain, but absent from some lines, and still blind to cross-session duplicates.
- **Content hash of the usage block**: rejected — two genuinely distinct responses with identical token counts are common at low token values, so this silently under-counts.
- **`message.id`, session-scoped**: rejected — fixes the fan-out but not the resume-copying. It is the half-measure that looks correct until a Session is resumed.
- **`message.id`, globally scoped** (chosen): the provider assigns `msg_…` per API response and it survives being copied into another transcript, which is exactly the property required.

pi has no provider-assigned message id, so its key is `sha256(entry_id | timestamp | model | totalTokens)`. All four components were verified byte-identical across fork-copies, and the composite is specific enough that distinct Messages do not collide.

## Consequences

- Ingest is idempotent by construction, which is what makes crash recovery, watermark resume, re-reading rotated files, and future Postgres replays all trivially safe. `architecture.md` §8 already claims this invariant; this key is what earns it.
- The Claude Code parser must collapse a content-block run into a single Message while streaming, emitting the usage once and treating later lines of the same `message.id` as tool activity only.
- First write wins on conflict. A Message copied into a resumed Session is attributed to the Session that was ingested first, so Session-level rollups are stable across re-runs but are *not* a partition of total spend — a copied Message counts toward one Session, not both.
- Two cheap external checks stay valid indefinitely: raw `"type":"assistant"` line counts should exceed stored Claude Message rows by roughly the fan-out ratio, and `SUM(reported_cost_usd)` for pi should fall *below* a naive `jq` sum over the raw files by exactly the fork-duplicate spend. If either comes out equal, dedup has regressed.
