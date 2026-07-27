# Validation record — increment 1, 2026-07-26

First run of the collector against the real transcript corpus on `VPCCA15FB`, plus
the independent cross-checks from `architecture.md` §7. Recorded here rather than
left in a terminal, because the interesting result is the one that *disagreed*.

## Run profile

| | |
|---|---|
| Files discovered | 1,197 |
| Lines read | 113,362 |
| Unparseable lines | **0** |
| Messages stored | 23,885 |
| Tool calls | 57,118 (28,588 merged rows) |
| Subagent runs | 810 |
| Sessions | 391 |
| Wall clock (cold backfill) | **12.6 s** |
| Peak RSS | **125 MB** |
| Steady-state run | **82 ms**, reading 2 of 1,197 files |
| DB size | 40 MB |
| Coverage | 2026-06-21 → 2026-07-26 |
| `tracker doctor` | 7/7 pass |

Peak RSS is comfortably inside the constraint that shaped this design
(`architecture.md` §1: 7.7 GB laptop with earlyoom). The `MemoryMax=512M` in the
systemd unit is ~4× headroom over the measured peak.

## Cross-check 1 — the fan-out collapse

Raw `"type":"assistant"` lines across `~/.claude/projects`: **49,988**.
Stored Claude Message rows: **20,151** — **40.3%**.

That ratio *is* the check. A figure near 100% would mean the content-block collapse
(ADR-0005) had regressed and every response was being billed once per block.

## Cross-check 2 — pi cost against raw `jq`

pi reports its own cost, so the raw files are authoritative and this check shares
no code with the implementation.

| | Raw `jq` | Stored | Delta |
|---|---:|---:|---:|
| Main-thread assistant | $26.691137 | $26.582818 | **−$0.108320 (−0.41%)** |
| Nested subagent sessions | $0.655203 | $0.655203 | $0.000000 |
| Compaction entries | $0.00012285 | $0.000123 | $0 |

The main-thread delta is the **fork-duplicate spend** — the same entry copied into
a resumed session — being correctly deduped. The direction matters: stored must be
*below* raw. Nested subagent sessions match exactly, confirming the five-level-deep
discovery path.

> A first attempt at this check reported stored *above* raw. The cause was the
> check, not the code: the `jq` scanned only depth-2 files and so omitted the 7
> nested subagent sessions the collector correctly ingests. Reconciled above.

## Cross-check 3 — `ccusage`, and what it actually showed

Run once via `npx -y ccusage@latest daily --json --since 20260720`, per
`architecture.md` §7. **It did not corroborate the token figures, and the
divergence is not a tracker defect** — so it is written down rather than quietly
dropped.

For Claude Code since 2026-07-20, three independent measurements:

| Metric | tracker | raw `jq` (deduped on `message.id`) | `ccusage` |
|---|---:|---:|---:|
| input | 95,713 | 95,779 | **10,073,645** |
| output | 4,000,095 | 4,035,047 | 5,763,922 |
| cache read | 757,047,958 | 770,970,584 | 778,842,549 |
| cache write | 22,402,039 | 22,564,812 | 20,941,775 |
| cost USD | $689.30 | — | $723.79 |

**The tracker agrees with the raw `jq` dedup and `ccusage` does not.** `ccusage`
reports an `inputTokens` figure ~100× larger than the value actually present in
the transcripts under `message.usage.input_tokens`, so that label denotes a
different quantity in its output — it is not a like-for-like comparison, and
treating it as one would have meant "fixing" a correct implementation.

The residual tracker↔`jq` gap was chased to exhaustion rather than accepted: it
was **ingest lag from the session running at the time**. Immediately after a fresh
`tracker collect`, distinct message ids were **5,181 (tracker) vs 5,182 (`jq`)** —
one message, written between the two commands.

The ~4.8% cost gap against `ccusage` is unexplained and most plausibly a
rate-table difference (this project prices `claude-fable-5` at its real $10/$50,
and covers `claude-opus-5`). It is **not** resolved here.

**Conclusion:** treat the raw-`jq` comparison as the regression check — it reads
the same field the cost math reads. `ccusage` is not wired into CI or the
collector; importing a Node dependency into a Python project for a check that does
not compare like with like is not worth the coupling.

## Defects this run surfaced

Four, none of which unit tests could have caught, because each was a mismatch
between the documented format and the real one:

1. **304 subagent transcripts were invisible.** Workflow-spawned subagents nest at
   `subagents/workflows/wf_<id>/agent-*.jsonl`; a direct-children glob missed them
   and ~1,800 billed messages with them. Discovery now recurses, filtering on the
   `agent-` prefix so the sibling `journal.jsonl` bookkeeping files stay out.
2. **2,453 lines were miscounted as corruption.** Claude Code's
   `file-history-delta` / `file-history-snapshot` lines carry no `sessionId`, and
   the parser validated before dispatching on line type. The count is the alarm
   for schema drift, so 2,453 false positives would have masked a real one. Now 0.
3. **Inode reuse defeated rotation detection.** Delete-and-recreate frequently
   returns the same inode, so an in-place rewrite at identical length would have
   been read from a stale EOF offset and its new content silently lost. Added an
   explicit `REWRITTEN` case.
4. **`claude-fable-5` was nearly priced as an Opus alias.** It is a real
   first-party model at $10/$50 per MTok; aliasing it would have halved $63 of
   measured spend.

## Reproducing

```bash
tracker collect --max-seconds 0 --full-rebuild
tracker doctor && tracker status
tracker report --models --since 30d
```
