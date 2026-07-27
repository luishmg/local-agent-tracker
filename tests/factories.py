"""
Synthetic transcript builders — the executable specification of the on-disk formats.

Fixtures are generated from field *shape*, never copied from real transcripts.
That is a security requirement, not a stylistic one: architecture.md §8 forbids
persisting conversation content, and a scrubber over real files is one regex away
from leaking while being unfalsifiable to review. Every string of "content" here
is a fixed literal.

It is also what makes the format legible. When Claude Code or pi changes shape,
this one file changes and the parser tests tell you what broke.

Shapes verified against live files on 2026-07-26:

  Claude assistant line
    top-level: type, uuid, parentUuid, sessionId, requestId, timestamp, cwd,
               gitBranch, version, userType, isSidechain, agentId,
               attributionAgent, slug, entrypoint, message
    message:   id, model, role, type, content[], stop_reason, usage{...}

  pi assistant entry
    top-level: type, id, parentId, timestamp
    message:   role, api, provider, model, responseId, responseModel,
               stopReason, timestamp, content[], usage{input, output, cacheRead,
               cacheWrite, totalTokens, reasoning?, cost{...}}

  pi compaction entry
    type, id, parentId, timestamp, summary, firstKeptEntryId, tokensBefore,
    fromHook, details{readFiles[], modifiedFiles[]}, usage{...}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Every fixture's "conversation content". Tests that assert no content reaches
#: the database look for this exact string, so it must never be varied casually.
CANARY = "CANARY-SECRET-STRING"

DEFAULT_TS = "2026-07-01T10:00:00.000Z"
DEFAULT_TS_EPOCH_MS = 1782900000000


# --------------------------------------------------------------------------- #
# Claude Code
# --------------------------------------------------------------------------- #

def claude_usage(
    *,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 0,
    cache_5m: int = 0,
    cache_1h: int = 0,
    iterations: int = 1,
) -> dict[str, Any]:
    """A Claude `message.usage` block.

    `iterations` models retries: the array holds one entry per underlying API call
    in the turn, and the top-level counts are the billed total, so
    `retry_count = len(iterations) - 1`.
    """
    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_5m + cache_1h,
        "cache_read_input_tokens": cache_read,
        "cache_creation": {
            "ephemeral_5m_input_tokens": cache_5m,
            "ephemeral_1h_input_tokens": cache_1h,
        },
        "service_tier": "standard",
    }
    if iterations >= 1:
        usage["iterations"] = [
            {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_5m + cache_1h,
                "cache_read_input_tokens": cache_read,
            }
            for _ in range(iterations)
        ]
    return usage


def claude_assistant_lines(
    *,
    msg_id: str = "msg_test0001",
    session_id: str = "sess-0001",
    request_id: str | None = "req_test0001",
    model: str = "claude-opus-4-8",
    blocks: tuple[str, ...] = ("thinking", "text", "tool_use"),
    tool_names: tuple[str, ...] = ("Read",),
    ts: str = DEFAULT_TS,
    cwd: str = "/home/user/Projects/demo",
    git_branch: str = "main",
    version: str = "2.0.0",
    stop_reason: str | None = "tool_use",
    agent_id: str | None = None,
    attribution_agent: str | None = None,
    is_sidechain: bool = False,
    usage: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """One API response, as Claude Code actually writes it: N lines, one per
    content block, each carrying a distinct `uuid` and an IDENTICAL complete
    `usage`.

    This fan-out is the single most important thing the parser must get right —
    summing per line inflates cost by the number of blocks (ADR-0005).
    """
    usage = usage if usage is not None else claude_usage()
    lines: list[dict[str, Any]] = []
    tool_iter = iter(tool_names)

    for i, block_type in enumerate(blocks):
        if block_type == "thinking":
            content = [{"type": "thinking", "thinking": f"{CANARY} reasoning", "signature": "sig"}]
        elif block_type == "text":
            content = [{"type": "text", "text": f"{CANARY} visible answer"}]
        elif block_type == "tool_use":
            name = next(tool_iter, "Read")
            content = [{
                "type": "tool_use",
                "id": f"toolu_{msg_id}_{i}",
                "name": name,
                "input": {"file_path": f"/{CANARY}/path"},
            }]
        else:
            raise ValueError(f"unknown block type {block_type!r}")

        line: dict[str, Any] = {
            "type": "assistant",
            "uuid": f"{msg_id}-line-{i}",
            "parentUuid": f"{msg_id}-line-{i - 1}" if i else None,
            "sessionId": session_id,
            "timestamp": ts,
            "cwd": cwd,
            "gitBranch": git_branch,
            "version": version,
            "userType": "external",
            "isSidechain": is_sidechain,
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": content,
                "stop_reason": stop_reason,
                "usage": usage,  # identical on every line, by design
            },
        }
        if request_id is not None:
            line["requestId"] = request_id
        if agent_id is not None:
            line["agentId"] = agent_id
        if attribution_agent is not None:
            line["attributionAgent"] = attribution_agent
        lines.append(line)

    return lines


def claude_user_line(
    *,
    session_id: str = "sess-0001",
    tool_use_id: str | None = None,
    is_error: bool | None = None,
    ts: str = DEFAULT_TS,
) -> dict[str, Any]:
    """A `type: "user"` line, which is how tool *results* come back."""
    if tool_use_id is None:
        content: Any = [{"type": "text", "text": f"{CANARY} user prompt"}]
    else:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": f"{CANARY} tool output",
        }
        if is_error is not None:
            block["is_error"] = is_error
        content = [block]

    return {
        "type": "user",
        "uuid": f"user-{tool_use_id or 'prompt'}",
        "sessionId": session_id,
        "timestamp": ts,
        "message": {"role": "user", "content": content},
    }


def claude_meta_json(
    *,
    agent_type: str = "Explore",
    description: str = f"{CANARY} explore the codebase",
    tool_use_id: str = "toolu_parent_1",
    spawn_depth: int = 1,
    model: str = "claude-haiku-4-5-20251001",
) -> dict[str, Any]:
    """The `.meta.json` sibling of a Claude subagent transcript."""
    return {
        "agentType": agent_type,
        "description": description,
        "toolUseId": tool_use_id,
        "spawnDepth": spawn_depth,
        "model": model,
    }


# --------------------------------------------------------------------------- #
# pi
# --------------------------------------------------------------------------- #

def pi_session_header(
    *,
    session_id: str = "019f0000-0000-7000-8000-000000000000",
    cwd: str = "/home/user/Projects/demo",
    ts: str = DEFAULT_TS,
    version: int = 3,
) -> dict[str, Any]:
    return {"type": "session", "id": session_id, "timestamp": ts, "version": version, "cwd": cwd}


def pi_model_change(
    *,
    entry_id: str = "mc000001",
    parent_id: str | None = None,
    model_id: str = "moonshotai/kimi-k2",
    provider: str = "openrouter",
    ts: str = DEFAULT_TS,
) -> dict[str, Any]:
    """pi records model switches as their own entry type. The compaction parser
    depends on this: a compaction entry has no `model` of its own and must inherit
    the session's model as of that point."""
    return {
        "type": "model_change",
        "id": entry_id,
        "parentId": parent_id,
        "modelId": model_id,
        "provider": provider,
        "timestamp": ts,
    }


def pi_assistant_entry(
    *,
    entry_id: str = "a1000001",
    parent_id: str | None = None,
    model: str = "moonshotai/kimi-k2",
    provider: str = "openrouter",
    api: str = "chat-completions",
    input_tokens: int = 9622,
    output_tokens: int = 301,
    reasoning: int | None = 132,
    cache_read: int = 0,
    cache_write: int = 0,
    cost_total: float = 0.00123,
    stop_reason: str = "stop",
    ts: str = DEFAULT_TS,
    ts_epoch_ms: int = DEFAULT_TS_EPOCH_MS,
    tool_calls: tuple[tuple[str, str], ...] = (),
    total_tokens: int | None = None,
) -> dict[str, Any]:
    """One pi assistant Message.

    The default token vector is taken from a real row and satisfies
    `totalTokens == input + output + cacheRead + cacheWrite` *while* `reasoning`
    is non-zero — the arithmetic proving reasoning is a subset of output, not an
    additional charge (see §1.3 of the plan, and `TokenUsage`).
    """
    usage: dict[str, Any] = {
        "input": input_tokens,
        "output": output_tokens,
        "cacheRead": cache_read,
        "cacheWrite": cache_write,
        "totalTokens": (
            total_tokens
            if total_tokens is not None
            else input_tokens + output_tokens + cache_read + cache_write
        ),
        "cost": {
            "input": round(cost_total * 0.6, 8),
            "output": round(cost_total * 0.4, 8),
            "cacheRead": 0.0,
            "cacheWrite": 0.0,
            "total": cost_total,
        },
    }
    if reasoning is not None:
        usage["reasoning"] = reasoning

    content: list[dict[str, Any]] = [{"type": "text", "text": f"{CANARY} answer"}]
    for call_id, name in tool_calls:
        content.append({
            "type": "toolCall",
            "id": call_id,
            "name": name,
            "arguments": {"path": f"/{CANARY}/file"},
        })

    return {
        "type": "message",
        "id": entry_id,
        "parentId": parent_id,
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "api": api,
            "provider": provider,
            "model": model,
            "responseModel": model,
            "responseId": f"resp-{entry_id}",
            "content": content,
            "usage": usage,
            "stopReason": stop_reason,
            "timestamp": ts_epoch_ms,
        },
    }


def pi_tool_result_entry(
    *,
    entry_id: str = "tr000001",
    parent_id: str | None = None,
    tool_call_id: str = "call_0001",
    tool_name: str = "read",
    is_error: bool = False,
    ts: str = DEFAULT_TS,
) -> dict[str, Any]:
    return {
        "type": "message",
        "id": entry_id,
        "parentId": parent_id,
        "timestamp": ts,
        "message": {
            "role": "toolResult",
            "toolCallId": tool_call_id,
            "toolName": tool_name,
            "isError": is_error,
            "content": f"{CANARY} tool output",
            "details": {},
            "timestamp": DEFAULT_TS_EPOCH_MS,
        },
    }


def pi_compaction_entry(
    *,
    entry_id: str = "c1000001",
    parent_id: str | None = None,
    tokens_before: int = 120_000,
    with_usage: bool = True,
    input_tokens: int = 8000,
    output_tokens: int = 1200,
    reasoning: int = 40,
    cache_read: int = 0,
    cache_write: int = 0,
    cost_total: float = 0.0456,
    ts: str = DEFAULT_TS,
    read_files: int = 3,
    modified_files: int = 1,
) -> dict[str, Any]:
    """A pi compaction entry — real, billable spend attached to no Message.

    Two traps live here. It carries no `model`, so cost attribution needs the
    session's running model state. And `summary` is verbatim conversation content
    that must be discarded, which is why it carries the canary.

    `with_usage=False` reproduces the older entries that have no usage block.
    """
    entry: dict[str, Any] = {
        "type": "compaction",
        "id": entry_id,
        "parentId": parent_id,
        "timestamp": ts,
        "summary": f"{CANARY} the conversation so far covered ...",
        "firstKeptEntryId": "a1000001",
        "tokensBefore": tokens_before,
        "fromHook": False,
        "details": {
            "readFiles": [f"/{CANARY}/read-{i}.py" for i in range(read_files)],
            "modifiedFiles": [f"/{CANARY}/mod-{i}.py" for i in range(modified_files)],
        },
    }
    if with_usage:
        entry["usage"] = {
            "input": input_tokens,
            "output": output_tokens,
            "reasoning": reasoning,
            "cacheRead": cache_read,
            "cacheWrite": cache_write,
            "totalTokens": input_tokens + output_tokens + cache_read + cache_write,
            "cost": {
                "input": round(cost_total * 0.7, 8),
                "output": round(cost_total * 0.3, 8),
                "cacheRead": 0.0,
                "cacheWrite": 0.0,
                "total": cost_total,
            },
        }
    return entry


def pi_run_history_line(
    *,
    agent: str = "Explore",
    task: str = f"{CANARY} map the repo",
    ts: int = 1782900000,
    status: str = "ok",
    duration: int = 4210,
    exit_code: int | None = None,
) -> dict[str, Any]:
    line: dict[str, Any] = {
        "agent": agent,
        "task": task,
        "ts": ts,
        "status": status,
        "duration": duration,
    }
    if exit_code is not None:
        line["exit"] = exit_code
    return line


# --------------------------------------------------------------------------- #
# Writing fixtures to disk
# --------------------------------------------------------------------------- #

def write_jsonl(path: Path, entries: list[dict[str, Any]], *, trailing_newline: bool = True) -> Path:
    """Write entries as JSONL.

    `trailing_newline=False` reproduces a file mid-append — the collector reads
    files the agents are actively writing to, and the watermark must refuse to
    advance past an incomplete final line.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(e) for e in entries)
    if trailing_newline and body:
        body += "\n"
    path.write_text(body, encoding="utf-8")
    return path


def append_jsonl(path: Path, entries: list[dict[str, Any]], *, trailing_newline: bool = True) -> Path:
    body = "\n".join(json.dumps(e) for e in entries)
    if trailing_newline and body:
        body += "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(body)
    return path


def claude_session_file(
    root: Path,
    *,
    project_slug: str = "-home-user-Projects-demo",
    session_id: str = "sess-0001",
    entries: list[dict[str, Any]] | None = None,
) -> Path:
    """Write a Claude Code session transcript at its real path shape:
    `<projects>/<cwd-slug>/<session-uuid>.jsonl`."""
    if entries is None:
        entries = claude_assistant_lines(session_id=session_id)
    return write_jsonl(root / project_slug / f"{session_id}.jsonl", entries)


def claude_subagent_file(
    root: Path,
    *,
    project_slug: str = "-home-user-Projects-demo",
    session_id: str = "sess-0001",
    agent_id: str = "a4984de84563b051b",
    entries: list[dict[str, Any]] | None = None,
    meta: dict[str, Any] | None = None,
) -> Path:
    """Write a Claude subagent transcript plus its `.meta.json` sibling at
    `<projects>/<slug>/<session-uuid>/subagents/agent-<id>.jsonl`."""
    if entries is None:
        entries = claude_assistant_lines(
            msg_id=f"msg_{agent_id}", session_id=session_id,
            agent_id=agent_id, attribution_agent="Explore", is_sidechain=True,
        )
    directory = root / project_slug / session_id / "subagents"
    path = write_jsonl(directory / f"agent-{agent_id}.jsonl", entries)
    meta_payload = meta if meta is not None else claude_meta_json()
    (directory / f"agent-{agent_id}.meta.json").write_text(
        json.dumps(meta_payload), encoding="utf-8"
    )
    return path


def pi_session_file(
    root: Path,
    *,
    project_slug: str = "--home-user-Projects-demo--",
    session_id: str = "019f0000-0000-7000-8000-000000000000",
    ts_prefix: str = "2026-07-01T10-00-00-000Z",
    entries: list[dict[str, Any]] | None = None,
) -> Path:
    """Write a pi session at `<sessions>/<cwd-slug>/<iso-ts>_<uuid>.jsonl`."""
    if entries is None:
        entries = [pi_session_header(session_id=session_id), pi_assistant_entry()]
    return write_jsonl(root / project_slug / f"{ts_prefix}_{session_id}.jsonl", entries)


def pi_subagent_session_file(
    root: Path,
    *,
    project_slug: str = "--home-user-Projects-demo--",
    parent_session_dir: str = "2026-07-01T10-00-00-000Z_019f0000-0000-7000-8000-000000000000",
    tool_call_id: str = "b2954803",
    entries: list[dict[str, Any]] | None = None,
) -> Path:
    """Write a pi nested subagent session at
    `<sessions>/<slug>/<session>/<toolcall-id>/run-0/session.jsonl`.

    Five levels deep — a `<slug>/*.jsonl` glob misses these entirely.
    """
    if entries is None:
        entries = [
            pi_session_header(session_id=f"sub-{tool_call_id}"),
            pi_assistant_entry(entry_id=f"s{tool_call_id}"),
        ]
    path = root / project_slug / parent_session_dir / tool_call_id / "run-0" / "session.jsonl"
    return write_jsonl(path, entries)


def pi_claude_artifacts_file(
    root: Path,
    *,
    project_slug: str = "--home-user-Projects-demo--",
    stem: str = "cc-mrxwv2da-99229d09",
    kind: str = "events",
    entries: list[dict[str, Any]] | None = None,
) -> Path:
    """Write a `claude-code-artifacts/` file — Claude Code SDK stream-json output
    that lives under pi's sessions tree but is NOT a pi transcript.

    These carry real Claude-shaped usage for sessions that also exist under
    `~/.claude/projects`, so discovery must exclude them: routed to the pi parser
    they yield garbage, and routed anywhere they are duplicate spend.
    """
    if entries is None:
        entries = claude_assistant_lines(msg_id="msg_artifact_1", session_id="sess-artifact")
    path = root / project_slug / "claude-code-artifacts" / f"{stem}_{kind}.jsonl"
    return write_jsonl(path, entries)
