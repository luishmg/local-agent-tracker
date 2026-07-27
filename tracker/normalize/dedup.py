"""
The dedup invariant (ADR-0005), in one testable place.

Keys are globally scoped, never per-Session, because both agents copy Message
history into a new transcript when a Session is resumed or forked. They are also
prefixed by record kind so that two kinds can never collide in a shared key space
even if their underlying ids happen to match.

Claude Code supplies a provider message id (`msg_...`) that is stable across those
copies, so it is used directly. pi supplies only an 8-hex entry id that is unique
within a Session file but repeats across them, so its key is a hash over fields
that were verified byte-identical in fork-copies of the same entry.
"""

from __future__ import annotations

import hashlib
from typing import Any

#: Truncation length for hashed keys. 32 hex chars = 128 bits, far beyond any
#: birthday-collision concern at personal scale, and short enough to stay legible
#: in a `SELECT dedup_key` during debugging.
_HASH_LEN = 32


def _hash(*parts: Any) -> str:
    """Stable digest over the given parts.

    `None` and the empty string must not collapse to the same digest, so parts are
    tagged before joining -- otherwise a pi entry with no model would key the same
    as one whose model is literally ''.
    """
    tagged = "\x1f".join("\x00" if p is None else str(p) for p in parts)
    return hashlib.sha256(tagged.encode("utf-8")).hexdigest()[:_HASH_LEN]


def claude_message_key(message_id: str, request_id: str | None = None) -> str:
    """Key for one Claude Code API response.

    `message_id` is `message.id`, shared by every content-block line of the
    response. The line `uuid` is deliberately NOT used: one response spans several
    lines with distinct uuids, and keying on it bills the response once per block.
    """
    if not message_id:
        raise ValueError("claude_message_key requires a non-empty message.id")
    return f"cc:{message_id}|{request_id or ''}"


def pi_message_key(
    entry_id: str,
    timestamp: str,
    model: str | None,
    total_tokens: int | None,
) -> str:
    """Key for one pi assistant Message.

    pi assigns no provider message id, so identity is a composite. All four
    components were verified byte-identical between fork-copies of the same entry.
    """
    if not entry_id:
        raise ValueError("pi_message_key requires a non-empty entry id")
    return f"pi:{_hash(entry_id, timestamp, model, total_tokens)}"


def pi_compaction_key(entry_id: str, timestamp: str, tokens_before: int | None) -> str:
    """Key for a pi compaction entry.

    Separate from `pi_message_key` because compaction entries may carry no `usage`
    block at all (older ones do not), so `total_tokens` is not available as a
    component. `tokensBefore` is present on all of them.
    """
    if not entry_id:
        raise ValueError("pi_compaction_key requires a non-empty entry id")
    return f"pic:{_hash(entry_id, timestamp, tokens_before)}"


def tool_call_key(agent: str, session_id: str, tool_use_id: str) -> str:
    """Key for one tool invocation.

    Session-scoped, unlike Messages: a tool_use id is only unique within a Session,
    and unlike Message spend a duplicated tool call in a resumed transcript is the
    same call being described twice, which this key correctly merges.
    """
    if not tool_use_id:
        raise ValueError("tool_call_key requires a non-empty tool_use id")
    return f"tc:{agent}|{session_id}|{tool_use_id}"


def pi_run_history_key(
    agent_name: str | None,
    ts: Any,
    duration_ms: Any,
    status: str | None,
) -> str:
    """Key for a line of `~/.pi/agent/run-history.jsonl`, which carries no id."""
    return f"rh:{_hash(agent_name, ts, duration_ms, status)}"


def claude_subagent_run_key(session_id: str, agent_run_id: str) -> str:
    """Key for a Claude subagent run.

    Subagent transcripts carry the PARENT `sessionId`, so `agentId` is what
    distinguishes one sub-thread from another within that Session.
    """
    if not agent_run_id:
        raise ValueError("claude_subagent_run_key requires a non-empty agentId")
    return f"sa:{session_id}|{agent_run_id}"


def pi_subagent_run_key(parent_session_id: str, tool_call_id: str) -> str:
    """Key for a pi nested subagent session, identified by its directory path."""
    if not tool_call_id:
        raise ValueError("pi_subagent_run_key requires a non-empty tool call id")
    return f"psa:{parent_session_id}|{tool_call_id}"


def task_digest(text: str | None) -> tuple[str | None, int | None]:
    """Hash a subagent task prompt for storage, returning `(sha256_16, length)`.

    architecture.md §8 forbids persisting conversation content, and a task prompt
    is conversation content. The digest still supports 'is this the same task as
    that one', which is all the reports need.
    """
    if text is None:
        return None, None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16], len(text)
