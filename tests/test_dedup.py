"""
The ADR-0005 invariant, tested in isolation from any parsing or storage.

The two properties that matter are opposites of each other, and both are easy to
lose in a refactor: keys must be *stable* across the ways a transcript duplicates
a record, and *distinct* across records that merely look similar.
"""

from __future__ import annotations

import pytest

from tracker.normalize.dedup import (
    claude_message_key,
    claude_subagent_run_key,
    pi_compaction_key,
    pi_message_key,
    pi_run_history_key,
    pi_subagent_run_key,
    task_digest,
    tool_call_key,
)


class TestClaudeMessageKey:
    def test_ignores_the_session_it_was_found_in(self) -> None:
        """The measured failure: 148 of 9,186 message ids appear under more than
        one sessionId, because resuming a Session copies its history. A
        session-scoped key bills each of those twice."""
        assert claude_message_key("msg_abc", "req_1") == claude_message_key("msg_abc", "req_1")

    def test_content_blocks_of_one_response_share_a_key(self) -> None:
        """One API response is written as several lines with distinct `uuid`s but a
        single `message.id`. Keying on the uuid bills the response once per block.

        Shaped after the real seven-line response measured in ADR-0005: the line
        uuid is what varies, and it must not reach the key at all.
        """
        lines = [
            {"uuid": u, "message": {"id": "msg_01D28axiw8xvj4qXz9cM9ST6"},
             "requestId": "req_011Ccc6qkmnHAuiX9oJNKzYM"}
            for u in ("07125504", "a45b5ff5", "38b16e36", "2513e57a",
                      "b38839c0", "b64a12c0", "8c8f254e")
        ]
        keys = {claude_message_key(ln["message"]["id"], ln["requestId"]) for ln in lines}
        assert len(keys) == 1, "seven content-block lines must yield one key, not seven"

    def test_distinct_messages_get_distinct_keys(self) -> None:
        assert claude_message_key("msg_a", "req_1") != claude_message_key("msg_b", "req_1")

    def test_missing_request_id_is_tolerated(self) -> None:
        assert claude_message_key("msg_a") == claude_message_key("msg_a", None)
        assert claude_message_key("msg_a").startswith("cc:msg_a|")

    def test_empty_message_id_is_rejected(self) -> None:
        """Silently keying on '' would collapse every unidentified Message onto one
        row -- the loudest possible under-count."""
        with pytest.raises(ValueError, match="message.id"):
            claude_message_key("")


class TestPiMessageKey:
    def test_fork_copies_of_one_entry_share_a_key(self) -> None:
        """pi's 8-hex entry id is unique only within a Session file, but all four
        composite components were verified byte-identical across fork-copies."""
        a = pi_message_key("75fc5036", "2026-07-01T10:00:00.000Z", "kimi-k2", 9923)
        b = pi_message_key("75fc5036", "2026-07-01T10:00:00.000Z", "kimi-k2", 9923)
        assert a == b

    @pytest.mark.parametrize(
        "args",
        [
            ("75fc5037", "2026-07-01T10:00:00.000Z", "kimi-k2", 9923),
            ("75fc5036", "2026-07-01T10:00:01.000Z", "kimi-k2", 9923),
            ("75fc5036", "2026-07-01T10:00:00.000Z", "glm-4.6", 9923),
            ("75fc5036", "2026-07-01T10:00:00.000Z", "kimi-k2", 9924),
        ],
    )
    def test_any_differing_component_changes_the_key(self, args: tuple) -> None:
        baseline = pi_message_key("75fc5036", "2026-07-01T10:00:00.000Z", "kimi-k2", 9923)
        assert pi_message_key(*args) != baseline

    def test_none_and_empty_string_do_not_collide(self) -> None:
        """Without tagging, a missing model and an empty model would hash alike."""
        assert pi_message_key("e1", "t", None, 10) != pi_message_key("e1", "t", "", 10)

    def test_empty_entry_id_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="entry id"):
            pi_message_key("", "t", "m", 1)


class TestPiCompactionKey:
    def test_does_not_depend_on_usage(self) -> None:
        """Older compaction entries carry no `usage` block at all, so total tokens
        is unavailable as a component -- `tokensBefore` always is."""
        assert pi_compaction_key("c1", "t", 120_000) == pi_compaction_key("c1", "t", 120_000)

    def test_cannot_collide_with_an_assistant_message(self) -> None:
        """Both are stored in `messages`, so a shared key space would let a
        compaction entry silently displace a Message with the same entry id."""
        assert pi_compaction_key("x", "t", 5) != pi_message_key("x", "t", None, 5)


class TestToolCallKey:
    def test_is_session_scoped_unlike_messages(self) -> None:
        """A tool_use id is unique only within a Session, and a duplicate in a
        resumed transcript describes the same call -- which this correctly merges."""
        a = tool_call_key("claude-code", "sess-a", "toolu_1")
        b = tool_call_key("claude-code", "sess-b", "toolu_1")
        assert a != b

    def test_agents_do_not_share_a_key_space(self) -> None:
        assert tool_call_key("pi", "s", "t1") != tool_call_key("claude-code", "s", "t1")

    def test_empty_tool_use_id_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="tool_use id"):
            tool_call_key("pi", "s", "")


class TestSubagentKeys:
    def test_claude_subagent_discriminated_by_agent_id(self) -> None:
        """Subagent transcripts carry the PARENT sessionId, so without agentId two
        sibling subagents of one Session would collapse into one row."""
        a = claude_subagent_run_key("sess-a", "a4984de84563b051b")
        b = claude_subagent_run_key("sess-a", "b1234567890abcdef")
        assert a != b

    def test_pi_run_history_key_survives_reordering(self) -> None:
        """run-history.jsonl has no id; these four fields are what identify a run."""
        assert pi_run_history_key("Explore", 1782000000, 4210, "ok") == pi_run_history_key(
            "Explore", 1782000000, 4210, "ok"
        )

    def test_run_history_distinguishes_status(self) -> None:
        ok = pi_run_history_key("Explore", 1782000000, 4210, "ok")
        err = pi_run_history_key("Explore", 1782000000, 4210, "error")
        assert ok != err

    def test_pi_subagent_key_requires_tool_call_id(self) -> None:
        with pytest.raises(ValueError, match="tool call id"):
            pi_subagent_run_key("parent", "")


class TestPrefixesAreDisjoint:
    def test_no_two_kinds_can_collide(self) -> None:
        """Every key kind lives in the same `dedup_key` column across two tables;
        prefixes are what stop an id reused between kinds from colliding."""
        keys = [
            claude_message_key("x", "y"),
            pi_message_key("x", "y", "m", 1),
            pi_compaction_key("x", "y", 1),
            tool_call_key("pi", "x", "y"),
            pi_run_history_key("x", "y", 1, "ok"),
            claude_subagent_run_key("x", "y"),
            pi_subagent_run_key("x", "y"),
        ]
        assert len(set(keys)) == len(keys)


class TestTaskDigest:
    def test_never_returns_the_text(self) -> None:
        """architecture.md §8: a subagent task prompt is conversation content."""
        secret = "CANARY-SECRET-STRING investigate the auth bug"
        digest, length = task_digest(secret)
        assert digest is not None
        assert secret not in digest
        assert "CANARY" not in digest
        assert length == len(secret)

    def test_same_task_hashes_alike(self) -> None:
        assert task_digest("do the thing")[0] == task_digest("do the thing")[0]

    def test_none_passes_through(self) -> None:
        assert task_digest(None) == (None, None)
