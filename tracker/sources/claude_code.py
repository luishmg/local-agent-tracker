"""
Claude Code transcript parser.

Sole owner of Claude's field names. Nothing outside this module may reference
`input_tokens`, `cache_creation_input_tokens`, `stop_reason`, or the line `uuid`.

The one thing this file must not get wrong
------------------------------------------
Claude Code writes a single API response as **several JSONL lines, one per content
block**, and repeats the complete `message.usage` on every one of them. Measured:
755 assistant lines carried only 368 distinct `message.id`. Summing usage per line
inflates cost by the block count — up to 7x on a tool-heavy turn.

So the parser keeps a per-file set of seen `message.id`s. The first line for an id
emits the Message with its usage; every later line of the same id contributes only
tool activity. Cross-file duplicates (a resumed Session copies history) are caught
downstream by the `dedup_key` primary key. See ADR-0005.

Claude Code has no cost field at all — `derived_cost_usd` is computed later from
the pricing table, and this module leaves cost untouched.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from tracker.normalize.dedup import (
    claude_message_key,
    claude_subagent_run_key,
    task_digest,
    tool_call_key,
)
from tracker.normalize.models import (
    NormalizedMessage,
    NormalizedSubagentRun,
    NormalizedToolCall,
    ParseResult,
    TokenUsage,
)

AGENT = "claude-code"

#: `<synthetic>` responses are generated locally and never billed. They must map
#: to an explicit zero rather than the "unknown model" path, which exists to warn
#: about genuinely missing pricing (ADR-0004).
SYNTHETIC_MODEL = "<synthetic>"


def parse_iso_ms(ts: str | None) -> int | None:
    """ISO-8601 to epoch milliseconds. Returns None rather than raising, because a
    single malformed timestamp must not abort a file (architecture.md §4.1)."""
    if not ts:
        return None
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _usage_from(raw: dict[str, Any]) -> TokenUsage:
    """Translate a Claude `usage` block into normalized counts.

    The 5m/1h cache-write split is priced differently and must not be merged
    (architecture.md §3.2). When the `cache_creation` object is absent — older
    transcripts — the total falls into the 5m bucket, which is Claude Code's
    default TTL.
    """
    creation = raw.get("cache_creation") or {}
    cache_write_total = int(raw.get("cache_creation_input_tokens") or 0)

    if creation:
        write_5m = int(creation.get("ephemeral_5m_input_tokens") or 0)
        write_1h = int(creation.get("ephemeral_1h_input_tokens") or 0)
    else:
        write_5m, write_1h = cache_write_total, 0

    return TokenUsage(
        input=int(raw.get("input_tokens") or 0),
        output=int(raw.get("output_tokens") or 0),
        reasoning=0,  # Claude bills thinking inside output_tokens
        cache_read=int(raw.get("cache_read_input_tokens") or 0),
        cache_write_5m=write_5m,
        cache_write_1h=write_1h,
        cache_write=cache_write_total or (write_5m + write_1h),
    )


def _retry_count(raw_usage: dict[str, Any]) -> int:
    """`iterations[]` holds one entry per underlying API call in the turn; the
    top-level usage is the billed total across them."""
    iterations = raw_usage.get("iterations")
    if isinstance(iterations, list) and iterations:
        return len(iterations) - 1
    return 0


def _project_slug(path: Path, root: Path | None) -> str | None:
    """The `<cwd-slug>` directory Claude Code files a session under."""
    try:
        rel = path.relative_to(root) if root else None
    except ValueError:
        rel = None
    if rel and rel.parts:
        return rel.parts[0]
    return path.parent.name or None


def _tool_calls_from_line(
    line: dict[str, Any],
    *,
    session_id: str,
    source_file: str,
    project_slug: str | None,
    ts: str | None,
    ts_epoch_ms: int | None,
) -> list[NormalizedToolCall]:
    """Extract `tool_use` blocks. Called for *every* line of a response, including
    the ones whose usage was already counted — that is where the tool calls live."""
    message = line.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        return []

    calls: list[NormalizedToolCall] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        tool_use_id = block.get("id")
        if not tool_use_id:
            continue
        calls.append(
            NormalizedToolCall(
                dedup_key=tool_call_key(AGENT, session_id, tool_use_id),
                agent=AGENT,
                session_id=session_id,
                tool_use_id=tool_use_id,
                tool_name=block.get("name"),
                agent_run_id=line.get("agentId"),
                project_slug=project_slug,
                ts=ts,
                ts_epoch_ms=ts_epoch_ms,
                source_file=source_file,
            )
        )
    return calls


def _tool_results_from_line(
    line: dict[str, Any],
    *,
    session_id: str,
    source_file: str,
    ts_epoch_ms: int | None,
) -> list[NormalizedToolCall]:
    """Extract `tool_result` blocks from a `user` line.

    `is_error` is frequently absent on successful results, so its absence is read
    as success rather than left unknown — an unset flag here means "no error was
    reported", which is what the reliability metrics need.
    """
    message = line.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        return []

    results: list[NormalizedToolCall] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        tool_use_id = block.get("tool_use_id")
        if not tool_use_id:
            continue
        results.append(
            NormalizedToolCall(
                dedup_key=tool_call_key(AGENT, session_id, tool_use_id),
                agent=AGENT,
                session_id=session_id,
                tool_use_id=tool_use_id,
                is_error=bool(block.get("is_error", False)),
                result_ts_epoch_ms=ts_epoch_ms,
                source_file=source_file,
            )
        )
    return results


def parse_lines(
    lines: Any,
    *,
    source_file: str,
    projects_root: Path | None = None,
    seen_message_ids: set[str] | None = None,
) -> ParseResult:
    """Parse an iterable of raw JSONL byte/str lines into normalized records.

    `seen_message_ids` is the content-block collapse state. Pass a caller-owned set
    to continue a file across an incremental read — a response's blocks can
    straddle the byte offset where the previous Collector Run stopped.
    """
    result = ParseResult()
    seen = seen_message_ids if seen_message_ids is not None else set()
    path = Path(source_file)
    project_slug = _project_slug(path, projects_root)
    subagent_seen: set[str] = set()

    for raw in lines:
        result.lines_read += 1
        try:
            line = json.loads(raw)
        except (ValueError, TypeError):
            result.lines_skipped += 1
            continue
        if not isinstance(line, dict):
            result.lines_skipped += 1
            continue

        line_type = line.get("type")

        # Dispatch on type BEFORE validating anything. Claude Code writes several
        # bookkeeping line types that carry no `sessionId` at all --
        # `file-history-delta` and `file-history-snapshot` account for thousands of
        # lines here. Validating first counted every one as corruption, which is
        # exactly the noise that would hide a real schema change.
        if line_type not in ("assistant", "user"):
            continue

        session_id = line.get("sessionId")
        ts = line.get("timestamp")
        ts_epoch_ms = parse_iso_ms(ts)

        if not session_id:
            result.lines_skipped += 1
            continue

        if line_type == "user":
            result.tool_calls.extend(
                _tool_results_from_line(
                    line, session_id=session_id, source_file=source_file,
                    ts_epoch_ms=ts_epoch_ms,
                )
            )
            continue

        message = line.get("message") or {}
        message_id = message.get("id")

        # Tool calls come from every line of the response, not just the first.
        result.tool_calls.extend(
            _tool_calls_from_line(
                line, session_id=session_id, source_file=source_file,
                project_slug=project_slug, ts=ts, ts_epoch_ms=ts_epoch_ms,
            )
        )

        if not message_id or ts is None or ts_epoch_ms is None:
            result.lines_skipped += 1
            continue

        # ---- the collapse (ADR-0005) -------------------------------------- #
        if message_id in seen:
            continue
        seen.add(message_id)

        raw_usage = message.get("usage") or {}
        model = message.get("model")
        agent_run_id = line.get("agentId")

        result.messages.append(
            NormalizedMessage(
                dedup_key=claude_message_key(message_id, line.get("requestId")),
                agent=AGENT,
                session_id=session_id,
                ts=ts,
                ts_epoch_ms=ts_epoch_ms,
                source_file=source_file,
                kind="assistant",
                usage=_usage_from(raw_usage),
                model=model,
                provider="anthropic",
                stop_reason=message.get("stop_reason"),
                agent_run_id=agent_run_id,
                subagent_type=line.get("attributionAgent"),
                is_sidechain=bool(line.get("isSidechain", False)),
                source_entry_id=line.get("uuid"),
                provider_msg_id=message_id,
                request_id=line.get("requestId"),
                parent_entry_id=line.get("parentUuid"),
                cwd=line.get("cwd"),
                project_slug=project_slug,
                git_branch=line.get("gitBranch"),
                agent_version=line.get("version"),
                retry_count=_retry_count(raw_usage),
                cache_miss_reason=(line.get("diagnostics") or {}).get("cache_miss_reason")
                if isinstance(line.get("diagnostics"), dict)
                else None,
                # Claude Code has no cost field; pricing fills these in later.
                reported_cost_usd=None,
            )
        )

        # A subagent transcript announces itself via agentId + isSidechain. One
        # run row per agentId, completed later from the .meta.json sibling.
        if agent_run_id and agent_run_id not in subagent_seen:
            subagent_seen.add(agent_run_id)
            result.subagent_runs.append(
                NormalizedSubagentRun(
                    dedup_key=claude_subagent_run_key(session_id, agent_run_id),
                    agent=AGENT,
                    source="claude-subagent-transcript",
                    source_file=source_file,
                    parent_session_id=session_id,
                    agent_run_id=agent_run_id,
                    agent_type=line.get("attributionAgent"),
                    started_at=ts,
                    model=model,
                )
            )

    return result


def parse_meta_json(
    raw: str | bytes,
    *,
    source_file: str,
    session_id: str,
    agent_run_id: str,
) -> NormalizedSubagentRun | None:
    """Parse a subagent `.meta.json` sibling.

    `description` is the task prompt — conversation content — so only its digest
    is kept (architecture.md §8).
    """
    try:
        meta = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(meta, dict):
        return None

    digest, length = task_digest(meta.get("description"))
    return NormalizedSubagentRun(
        dedup_key=claude_subagent_run_key(session_id, agent_run_id),
        agent=AGENT,
        source="claude-subagent-transcript",
        source_file=source_file,
        parent_session_id=session_id,
        agent_run_id=agent_run_id,
        agent_type=meta.get("agentType"),
        tool_use_id=meta.get("toolUseId"),
        spawn_depth=meta.get("spawnDepth"),
        task_sha256=digest,
        task_len=length,
        model=meta.get("model"),
    )
