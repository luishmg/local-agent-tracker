"""
The normalized record vocabulary.

These are `slots=True` dataclasses rather than Pydantic models on purpose: the
first backfill parses ~500k transcript lines, and per-record validation at that
volume would dominate runtime. Pydantic stays at the config and pricing
boundaries, where inputs are small and untrusted. The STRICT SQLite schema is
what catches type mistakes here.

Every field name below is agent-neutral. The mapping from Claude Code's
`input_tokens` / `stop_reason` and pi's `input` / `stopReason` happens in
`tracker.sources.*` and nowhere else -- `tests/test_no_raw_field_names_leak.py`
enforces that.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Agent = Literal["claude-code", "pi"]
MessageKind = Literal["assistant", "compaction"]
CostSource = Literal["reported", "derived", "zero-rated", "unknown"]
SubagentSource = Literal[
    "pi-run-history", "claude-subagent-transcript", "pi-subagent-session"
]


@dataclass(slots=True)
class TokenUsage:
    """Normalized token counts for one Message.

    `reasoning` is a SUBSET of `output`, not an addend. Verified against live pi
    data: `totalTokens == input + output + cacheRead + cacheWrite` holds exactly
    even when `reasoning` is non-zero. It is stored as a diagnostic breakdown and
    must never appear as a term in a cost formula.

    `cache_write_5m` and `cache_write_1h` are kept apart because they are priced
    differently (architecture.md §3.2); `cache_write` is their sum, retained for
    convenient rollups.
    """

    input: int = 0
    output: int = 0
    reasoning: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_write: int = 0
    total: int = 0

    def __post_init__(self) -> None:
        if self.cache_write == 0:
            self.cache_write = self.cache_write_5m + self.cache_write_1h
        if self.total == 0:
            self.total = self.input + self.output + self.cache_read + self.cache_write

    @property
    def context_tokens(self) -> int:
        """What the model actually had in front of it for this call."""
        return self.input + self.cache_read + self.cache_write


@dataclass(slots=True)
class NormalizedMessage:
    """One Message (CONTEXT.md) -- the finest grain stored.

    For Claude Code this is one API response collapsed from its several
    content-block lines; for pi it is one assistant entry or one compaction entry.
    """

    dedup_key: str
    agent: Agent
    session_id: str
    ts: str
    ts_epoch_ms: int
    source_file: str

    kind: MessageKind = "assistant"
    usage: TokenUsage = field(default_factory=TokenUsage)

    model: str | None = None
    response_model: str | None = None
    provider: str | None = None
    api: str | None = None
    stop_reason: str | None = None

    # Subagent attribution. Claude subagent transcripts carry the PARENT
    # sessionId, so `agent_run_id` (its `agentId`) is the sub-thread discriminator
    # and subagent spend rolls up into the parent Session automatically.
    agent_run_id: str | None = None
    subagent_type: str | None = None
    is_sidechain: bool = False
    parent_session_id: str | None = None
    spawn_depth: int | None = None

    source_entry_id: str | None = None
    provider_msg_id: str | None = None
    request_id: str | None = None
    parent_entry_id: str | None = None

    cwd: str | None = None
    project_slug: str | None = None
    git_branch: str | None = None
    agent_version: str | None = None

    context_tokens: int | None = None
    retry_count: int = 0
    cache_miss_reason: str | None = None

    #: pi writes this per Message; Claude Code has no cost field at all.
    reported_cost_usd: float | None = None
    #: Filled by the pricing stage, not the parsers.
    derived_cost_usd: float | None = None
    cost_usd: float | None = None
    cost_source: CostSource | None = None
    pricing_version: str | None = None

    def to_row(self, ingested_at: str) -> dict[str, Any]:
        """Flatten to the `messages` column layout."""
        return {
            "dedup_key": self.dedup_key,
            "agent": self.agent,
            "kind": self.kind,
            "session_id": self.session_id,
            "agent_run_id": self.agent_run_id,
            "subagent_type": self.subagent_type,
            "is_sidechain": int(self.is_sidechain),
            "parent_session_id": self.parent_session_id,
            "spawn_depth": self.spawn_depth,
            "source_file": self.source_file,
            "source_entry_id": self.source_entry_id,
            "provider_msg_id": self.provider_msg_id,
            "request_id": self.request_id,
            "parent_entry_id": self.parent_entry_id,
            "cwd": self.cwd,
            "project_slug": self.project_slug,
            "git_branch": self.git_branch,
            "agent_version": self.agent_version,
            "ts": self.ts,
            "ts_epoch_ms": self.ts_epoch_ms,
            "model": self.model,
            "response_model": self.response_model,
            "provider": self.provider,
            "api": self.api,
            "stop_reason": self.stop_reason,
            "input_tokens": self.usage.input,
            "output_tokens": self.usage.output,
            "reasoning_tokens": self.usage.reasoning,
            "cache_read_tokens": self.usage.cache_read,
            "cache_write_5m_tokens": self.usage.cache_write_5m,
            "cache_write_1h_tokens": self.usage.cache_write_1h,
            "cache_write_tokens": self.usage.cache_write,
            "total_tokens": self.usage.total,
            "context_tokens": (
                self.context_tokens
                if self.context_tokens is not None
                else self.usage.context_tokens
            ),
            "retry_count": self.retry_count,
            "latency_ms": None,  # filled by derive/latency.py after ingest
            "cache_miss_reason": self.cache_miss_reason,
            "reported_cost_usd": self.reported_cost_usd,
            "derived_cost_usd": self.derived_cost_usd,
            "cost_usd": self.cost_usd,
            "cost_source": self.cost_source,
            "pricing_version": self.pricing_version,
            "ingested_at": ingested_at,
        }


@dataclass(slots=True)
class NormalizedToolCall:
    """One tool invocation. The call and its result are separate transcript lines
    that merge onto this record's `dedup_key`, so most fields are optional --
    whichever line is parsed supplies its own half."""

    dedup_key: str
    agent: Agent
    session_id: str
    tool_use_id: str
    source_file: str

    tool_name: str | None = None
    agent_run_id: str | None = None
    project_slug: str | None = None
    ts: str | None = None
    ts_epoch_ms: int | None = None
    result_ts_epoch_ms: int | None = None
    is_error: bool | None = None

    def to_row(self, ingested_at: str) -> dict[str, Any]:
        duration = None
        if self.result_ts_epoch_ms is not None and self.ts_epoch_ms is not None:
            duration = self.result_ts_epoch_ms - self.ts_epoch_ms
        return {
            "dedup_key": self.dedup_key,
            "agent": self.agent,
            "session_id": self.session_id,
            "agent_run_id": self.agent_run_id,
            "tool_use_id": self.tool_use_id,
            "tool_name": self.tool_name,
            "project_slug": self.project_slug,
            "ts": self.ts,
            "ts_epoch_ms": self.ts_epoch_ms,
            "result_ts_epoch_ms": self.result_ts_epoch_ms,
            "duration_ms": duration,
            "is_error": None if self.is_error is None else int(self.is_error),
            "source_file": self.source_file,
            "ingested_at": ingested_at,
        }


@dataclass(slots=True)
class NormalizedSubagentRun:
    """One subagent run.

    `task_sha256`/`task_len` stand in for the task prompt, which is conversation
    content and must never be stored (architecture.md §8).
    """

    dedup_key: str
    agent: Agent
    source: SubagentSource
    source_file: str

    parent_session_id: str | None = None
    agent_run_id: str | None = None
    agent_type: str | None = None
    tool_use_id: str | None = None
    spawn_depth: int | None = None
    task_sha256: str | None = None
    task_len: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int | None = None
    status: str | None = None
    exit_code: int | None = None
    model: str | None = None
    message_count: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None

    def to_row(self, ingested_at: str) -> dict[str, Any]:
        row = asdict(self)
        row["ingested_at"] = ingested_at
        return row


@dataclass(slots=True)
class ParseResult:
    """Everything one source file yielded, plus how tolerant the parse had to be.

    architecture.md §4.1 requires malformed lines to be counted and skipped rather
    than fatal, so `lines_skipped` is part of the return value, not an exception.
    """

    messages: list[NormalizedMessage] = field(default_factory=list)
    tool_calls: list[NormalizedToolCall] = field(default_factory=list)
    subagent_runs: list[NormalizedSubagentRun] = field(default_factory=list)
    lines_read: int = 0
    lines_skipped: int = 0

    def extend(self, other: ParseResult) -> None:
        self.messages.extend(other.messages)
        self.tool_calls.extend(other.tool_calls)
        self.subagent_runs.extend(other.subagent_runs)
        self.lines_read += other.lines_read
        self.lines_skipped += other.lines_skipped

    def __bool__(self) -> bool:
        return bool(self.messages or self.tool_calls or self.subagent_runs)
