"""
pi session parser.

Sole owner of pi's field names. Nothing outside this module may reference
`input`/`output`/`cacheRead`/`cacheWrite`/`stopReason`/`totalTokens`.

Three things here differ from what architecture.md §3.1 describes, all verified
against live session files:

**`compaction` entries carry real, billable spend.** They are an undocumented
entry type with their own `usage` and `usage.cost`, attached to no assistant
Message. Dropping them under-reports pi cost. They carry no `model` of their own,
so the parser tracks the session's active model as it streams — hence the
`model_change` state machine below. Older compaction entries have no `usage` block
at all, so it is treated as optional.

**`usage.reasoning` is a SUBSET of `output`, not additional tokens.** Verified:
`input + output + cacheRead + cacheWrite == totalTokens` holds exactly even when
`reasoning` is non-zero (e.g. 9622 + 301 + 0 + 0 = 9923, with reasoning = 132).
It is stored as a diagnostic breakdown and must never be a term in a cost formula.

**pi reports its own cost.** `usage.cost.total` is the Reported Cost, so pi rows
need no pricing-table derivation at all.

Content that must never be stored: `compaction.summary` is a verbatim conversation
summary, and `details.readFiles`/`modifiedFiles` are filesystem paths from the
user's work. Only counts survive (architecture.md §8).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tracker.normalize.dedup import (
    pi_compaction_key,
    pi_message_key,
    tool_call_key,
)
from tracker.normalize.models import (
    NormalizedMessage,
    NormalizedToolCall,
    ParseResult,
    TokenUsage,
)
from tracker.sources.claude_code import parse_iso_ms

AGENT = "pi"


class SessionState:
    """Streaming state for one pi session file.

    pi splits information a Message needs across entry types: the `session` header
    holds `cwd`, and `model_change` entries hold the model that later compaction
    entries must inherit. Incremental reads mean a file can be parsed across
    several Collector Runs, so the caller owns this object and passes it back in.
    """

    __slots__ = ("session_id", "cwd", "active_model", "active_provider", "seen_entry_ids")

    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id
        self.cwd: str | None = None
        self.active_model: str | None = None
        self.active_provider: str | None = None
        self.seen_entry_ids: set[str] = set()


def _usage_from(raw: dict[str, Any]) -> TokenUsage:
    """Translate a pi `usage` block into normalized counts.

    pi reports a single `cacheWrite` with no TTL split, so it lands in the 5m
    bucket -- pi's providers do not offer Anthropic's 1h cache tier. `cacheWrite1h`
    is read when present, for the Anthropic-via-pi case.
    """
    write_1h = int(raw.get("cacheWrite1h") or 0)
    write_total = int(raw.get("cacheWrite") or 0)
    # `cacheWrite` is the total when a 1h figure is also reported.
    write_5m = max(write_total - write_1h, 0) if write_1h else write_total

    return TokenUsage(
        input=int(raw.get("input") or 0),
        output=int(raw.get("output") or 0),
        # Diagnostic only -- already inside `output`. Never a cost term.
        reasoning=int(raw.get("reasoning") or 0),
        cache_read=int(raw.get("cacheRead") or 0),
        cache_write_5m=write_5m,
        cache_write_1h=write_1h,
        cache_write=write_total or (write_5m + write_1h),
        total=int(raw.get("totalTokens") or 0),
    )


def _reported_cost(raw_usage: dict[str, Any]) -> float | None:
    cost = raw_usage.get("cost")
    if not isinstance(cost, dict):
        return None
    total = cost.get("total")
    return float(total) if isinstance(total, (int, float)) else None


def _project_slug(path: Path, root: Path | None) -> str | None:
    try:
        rel = path.relative_to(root) if root else None
    except ValueError:
        rel = None
    if rel and rel.parts:
        return rel.parts[0]
    return path.parent.name or None


def _tool_calls_from_content(
    content: Any,
    *,
    session_id: str,
    source_file: str,
    project_slug: str | None,
    ts: str | None,
    ts_epoch_ms: int | None,
    agent_run_id: str | None,
) -> list[NormalizedToolCall]:
    if not isinstance(content, list):
        return []
    calls: list[NormalizedToolCall] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "toolCall":
            continue
        call_id = block.get("id")
        if not call_id:
            continue
        calls.append(
            NormalizedToolCall(
                dedup_key=tool_call_key(AGENT, session_id, call_id),
                agent=AGENT,
                session_id=session_id,
                tool_use_id=call_id,
                tool_name=block.get("name"),
                agent_run_id=agent_run_id,
                project_slug=project_slug,
                ts=ts,
                ts_epoch_ms=ts_epoch_ms,
                source_file=source_file,
            )
        )
    return calls


def parse_lines(
    lines: Any,
    *,
    source_file: str,
    sessions_root: Path | None = None,
    state: SessionState | None = None,
    fallback_session_id: str | None = None,
    parent_session_id: str | None = None,
    agent_run_id: str | None = None,
) -> ParseResult:
    """Parse an iterable of raw JSONL lines from one pi session file.

    `parent_session_id` and `agent_run_id` are set for nested subagent sessions,
    which live at `<slug>/<session>/<toolcall-id>/run-0/session.jsonl` and are real
    spend that a flat `<slug>/*.jsonl` glob would miss entirely.
    """
    result = ParseResult()
    st = state if state is not None else SessionState(fallback_session_id)
    path = Path(source_file)
    project_slug = _project_slug(path, sessions_root)

    for raw in lines:
        result.lines_read += 1
        try:
            entry = json.loads(raw)
        except (ValueError, TypeError):
            result.lines_skipped += 1
            continue
        if not isinstance(entry, dict):
            result.lines_skipped += 1
            continue

        entry_type = entry.get("type")
        entry_id = entry.get("id")
        ts = entry.get("timestamp")
        ts_epoch_ms = parse_iso_ms(ts)

        # ---- header ------------------------------------------------------- #
        if entry_type == "session":
            st.session_id = entry_id or st.session_id
            st.cwd = entry.get("cwd") or st.cwd
            continue

        # ---- model state, which compaction entries inherit ----------------- #
        if entry_type == "model_change":
            st.active_model = entry.get("modelId") or st.active_model
            st.active_provider = entry.get("provider") or st.active_provider
            continue

        if entry_type == "thinking_level_change":
            continue

        session_id = st.session_id or fallback_session_id
        if not session_id or not entry_id or ts is None or ts_epoch_ms is None:
            if entry_type in ("message", "compaction"):
                result.lines_skipped += 1
            continue

        # ---- compaction: billable spend with no Message -------------------- #
        if entry_type == "compaction":
            raw_usage = entry.get("usage")
            if isinstance(raw_usage, dict):
                usage = _usage_from(raw_usage)
                reported = _reported_cost(raw_usage)
            else:
                # Compaction entries written before pi added usage reporting.
                usage, reported = TokenUsage(), None

            details = entry.get("details")
            details = details if isinstance(details, dict) else {}
            read_files = details.get("readFiles")
            modified_files = details.get("modifiedFiles")

            result.messages.append(
                NormalizedMessage(
                    dedup_key=pi_compaction_key(entry_id, ts, entry.get("tokensBefore")),
                    agent=AGENT,
                    session_id=session_id,
                    ts=ts,
                    ts_epoch_ms=ts_epoch_ms,
                    source_file=source_file,
                    kind="compaction",
                    usage=usage,
                    # No model of its own -- inherit the session's active model.
                    model=st.active_model,
                    provider=st.active_provider,
                    source_entry_id=entry_id,
                    parent_entry_id=entry.get("parentId"),
                    cwd=st.cwd,
                    project_slug=project_slug,
                    parent_session_id=parent_session_id,
                    agent_run_id=agent_run_id,
                    is_sidechain=agent_run_id is not None,
                    # tokensBefore is the context that was compacted away.
                    context_tokens=entry.get("tokensBefore"),
                    reported_cost_usd=reported,
                    cost_usd=reported,
                    cost_source="reported" if reported is not None else "unknown",
                    # `summary` and the file path lists are conversation content:
                    # only their shape is retained.
                    stop_reason=(
                        f"compaction:read={len(read_files) if isinstance(read_files, list) else 0},"
                        f"modified={len(modified_files) if isinstance(modified_files, list) else 0}"
                    ),
                )
            )
            continue

        if entry_type != "message":
            continue  # a future entry type is not corruption

        message = entry.get("message")
        if not isinstance(message, dict):
            result.lines_skipped += 1
            continue
        role = message.get("role")

        # ---- tool results -------------------------------------------------- #
        if role == "toolResult":
            call_id = message.get("toolCallId")
            if call_id:
                result.tool_calls.append(
                    NormalizedToolCall(
                        dedup_key=tool_call_key(AGENT, session_id, call_id),
                        agent=AGENT,
                        session_id=session_id,
                        tool_use_id=call_id,
                        tool_name=message.get("toolName"),
                        is_error=bool(message.get("isError", False)),
                        result_ts_epoch_ms=ts_epoch_ms,
                        agent_run_id=agent_run_id,
                        source_file=source_file,
                    )
                )
            continue

        if role != "assistant":
            continue  # user turns carry no usage

        result.tool_calls.extend(
            _tool_calls_from_content(
                message.get("content"),
                session_id=session_id,
                source_file=source_file,
                project_slug=project_slug,
                ts=ts,
                ts_epoch_ms=ts_epoch_ms,
                agent_run_id=agent_run_id,
            )
        )

        raw_usage = message.get("usage")
        if not isinstance(raw_usage, dict):
            continue  # an assistant entry with no usage is not billable

        usage = _usage_from(raw_usage)
        reported = _reported_cost(raw_usage)
        model = message.get("model") or st.active_model
        if model:
            st.active_model = model
        provider = message.get("provider") or st.active_provider

        result.messages.append(
            NormalizedMessage(
                dedup_key=pi_message_key(entry_id, ts, model, usage.total),
                agent=AGENT,
                session_id=session_id,
                ts=ts,
                ts_epoch_ms=ts_epoch_ms,
                source_file=source_file,
                kind="assistant",
                usage=usage,
                model=model,
                response_model=message.get("responseModel"),
                provider=provider,
                api=message.get("api"),
                stop_reason=message.get("stopReason"),
                source_entry_id=entry_id,
                parent_entry_id=entry.get("parentId"),
                cwd=st.cwd,
                project_slug=project_slug,
                parent_session_id=parent_session_id,
                agent_run_id=agent_run_id,
                is_sidechain=agent_run_id is not None,
                # pi writes its own cost, so no pricing-table derivation is needed.
                reported_cost_usd=reported,
                cost_usd=reported,
                cost_source="reported" if reported is not None else "unknown",
            )
        )

    return result
