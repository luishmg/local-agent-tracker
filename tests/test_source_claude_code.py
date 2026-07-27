"""
Claude Code parser.

The first test in this file is the most important one in the suite: it pins the
content-block collapse. Without it, every cost figure the product reports is
inflated by a factor that varies with tool density — which does not merely make
the numbers wrong, it makes the model-vs-model comparison they exist for invalid.
"""

from __future__ import annotations

import json

from tests.factories import (
    CANARY,
    claude_assistant_lines,
    claude_meta_json,
    claude_usage,
    claude_user_line,
)
from tracker.sources.claude_code import parse_iso_ms, parse_lines, parse_meta_json


def _raw(entries: list[dict]) -> list[str]:
    return [json.dumps(e) for e in entries]


class TestContentBlockFanout:
    def test_one_response_across_seven_lines_is_counted_once(self) -> None:
        """The measured shape from ADR-0005: thinking + text + 5 tool_use, each
        line carrying an identical complete usage."""
        usage = claude_usage(input_tokens=4054, output_tokens=3452)
        lines = claude_assistant_lines(
            msg_id="msg_01D28axiw8xvj4qXz9cM9ST6",
            request_id="req_011Ccc6qkmnHAuiX9oJNKzYM",
            blocks=("thinking", "text", "tool_use", "tool_use", "tool_use", "tool_use", "tool_use"),
            tool_names=("Read", "Bash", "Grep", "Edit", "Write"),
            usage=usage,
        )
        assert len(lines) == 7, "fixture must reproduce the fan-out"

        result = parse_lines(_raw(lines), source_file="/x.jsonl")

        assert len(result.messages) == 1
        message = result.messages[0]
        assert message.usage.input == 4054, "must be the single-response value, not 7x"
        assert message.usage.output == 3452
        assert message.usage.total == 4054 + 3452

    def test_all_tool_calls_are_kept_from_later_lines(self) -> None:
        """The collapse drops the duplicate *usage*, not the tool activity — tool
        calls live on the very lines whose usage is discarded."""
        lines = claude_assistant_lines(
            blocks=("thinking", "text", "tool_use", "tool_use", "tool_use"),
            tool_names=("Read", "Bash", "Grep"),
        )
        result = parse_lines(_raw(lines), source_file="/x.jsonl")

        assert len(result.messages) == 1
        assert {tc.tool_name for tc in result.tool_calls} == {"Read", "Bash", "Grep"}

    def test_two_distinct_responses_are_two_messages(self) -> None:
        """The collapse must not over-merge: distinct message ids stay distinct."""
        lines = [
            *claude_assistant_lines(msg_id="msg_a", blocks=("text",)),
            *claude_assistant_lines(msg_id="msg_b", blocks=("text",)),
        ]
        result = parse_lines(_raw(lines), source_file="/x.jsonl")
        assert {m.provider_msg_id for m in result.messages} == {"msg_a", "msg_b"}

    def test_collapse_state_survives_an_incremental_read(self) -> None:
        """A response's blocks can straddle the byte offset where the previous
        Collector Run stopped, so the seen-set is caller-owned."""
        lines = claude_assistant_lines(msg_id="msg_split", blocks=("thinking", "text", "tool_use"))
        seen: set[str] = set()

        first = parse_lines(_raw(lines[:1]), source_file="/x.jsonl", seen_message_ids=seen)
        second = parse_lines(_raw(lines[1:]), source_file="/x.jsonl", seen_message_ids=seen)

        assert len(first.messages) == 1
        assert len(second.messages) == 0, "the rest of the response is not a new Message"
        assert len(second.tool_calls) == 1, "but its tool call is still captured"


class TestFieldNormalization:
    def test_token_fields_map_to_normalized_names(self) -> None:
        lines = claude_assistant_lines(
            blocks=("text",),
            usage=claude_usage(
                input_tokens=10, output_tokens=20, cache_read=30, cache_5m=40, cache_1h=50
            ),
        )
        usage = parse_lines(_raw(lines), source_file="/x.jsonl").messages[0].usage

        assert usage.input == 10
        assert usage.output == 20
        assert usage.cache_read == 30
        assert usage.cache_write_5m == 40
        assert usage.cache_write_1h == 50
        assert usage.cache_write == 90
        assert usage.reasoning == 0, "Claude bills thinking inside output_tokens"

    def test_cache_write_split_is_preserved_not_merged(self) -> None:
        """5m and 1h are priced differently (architecture.md §3.2). Collapsing them
        into one number makes the difference unrecoverable at report time."""
        lines = claude_assistant_lines(
            blocks=("text",), usage=claude_usage(cache_5m=1000, cache_1h=7826)
        )
        usage = parse_lines(_raw(lines), source_file="/x.jsonl").messages[0].usage
        assert (usage.cache_write_5m, usage.cache_write_1h) == (1000, 7826)

    def test_absent_cache_creation_object_falls_back_to_5m(self) -> None:
        """Older transcripts have no `cache_creation` breakdown; 5m is the default
        TTL, so attributing the total there is the honest fallback."""
        raw_usage = {
            "input_tokens": 5,
            "output_tokens": 5,
            "cache_creation_input_tokens": 1234,
            "cache_read_input_tokens": 0,
        }
        lines = claude_assistant_lines(blocks=("text",), usage=raw_usage)
        usage = parse_lines(_raw(lines), source_file="/x.jsonl").messages[0].usage

        assert usage.cache_write_5m == 1234
        assert usage.cache_write_1h == 0
        assert usage.cache_write == 1234

    def test_context_metadata_is_captured(self) -> None:
        lines = claude_assistant_lines(
            blocks=("text",), cwd="/home/user/Projects/demo",
            git_branch="feature/x", version="2.1.0", stop_reason="end_turn",
        )
        m = parse_lines(_raw(lines), source_file="/x.jsonl").messages[0]

        assert m.cwd == "/home/user/Projects/demo"
        assert m.git_branch == "feature/x"
        assert m.agent_version == "2.1.0"
        assert m.stop_reason == "end_turn"
        assert m.provider == "anthropic"
        assert m.agent == "claude-code"

    def test_no_cost_is_ever_invented(self) -> None:
        """Claude Code transcripts carry no cost field; deriving it is the pricing
        stage's job, and guessing here would bypass ADR-0004's version stamping."""
        lines = claude_assistant_lines(blocks=("text",))
        m = parse_lines(_raw(lines), source_file="/x.jsonl").messages[0]

        assert m.reported_cost_usd is None
        assert m.derived_cost_usd is None
        assert m.cost_usd is None
        assert m.pricing_version is None


class TestRetriesAndReliability:
    def test_retry_count_is_iterations_minus_one(self) -> None:
        lines = claude_assistant_lines(blocks=("text",), usage=claude_usage(iterations=3))
        assert parse_lines(_raw(lines), source_file="/x.jsonl").messages[0].retry_count == 2

    def test_single_iteration_is_zero_retries(self) -> None:
        lines = claude_assistant_lines(blocks=("text",), usage=claude_usage(iterations=1))
        assert parse_lines(_raw(lines), source_file="/x.jsonl").messages[0].retry_count == 0

    def test_missing_iterations_is_zero_retries(self) -> None:
        usage = claude_usage(iterations=0)
        usage.pop("iterations", None)
        lines = claude_assistant_lines(blocks=("text",), usage=usage)
        assert parse_lines(_raw(lines), source_file="/x.jsonl").messages[0].retry_count == 0

    def test_tool_result_error_flag_is_captured(self) -> None:
        entries = [
            *claude_assistant_lines(blocks=("tool_use",), tool_names=("Bash",)),
            claude_user_line(tool_use_id="toolu_msg_test0001_0", is_error=True),
        ]
        result = parse_lines(_raw(entries), source_file="/x.jsonl")
        errors = [tc for tc in result.tool_calls if tc.is_error is True]
        assert len(errors) == 1

    def test_absent_is_error_reads_as_success(self) -> None:
        """Successful results frequently omit the flag entirely."""
        entries = [claude_user_line(tool_use_id="toolu_1")]
        result = parse_lines(_raw(entries), source_file="/x.jsonl")
        assert result.tool_calls[0].is_error is False


class TestSubagentAttribution:
    def test_sidechain_lines_carry_run_and_type(self) -> None:
        """Subagent transcripts use the PARENT sessionId, so agentId is the only
        thing distinguishing one sub-thread from another."""
        lines = claude_assistant_lines(
            blocks=("text",), session_id="parent-sess",
            agent_id="a4984de84563b051b", attribution_agent="Explore", is_sidechain=True,
        )
        result = parse_lines(_raw(lines), source_file="/x.jsonl")
        m = result.messages[0]

        assert m.session_id == "parent-sess", "subagent spend rolls up into the parent"
        assert m.agent_run_id == "a4984de84563b051b"
        assert m.subagent_type == "Explore"
        assert m.is_sidechain is True

    def test_main_thread_lines_are_not_sidechains(self) -> None:
        lines = claude_assistant_lines(blocks=("text",))
        m = parse_lines(_raw(lines), source_file="/x.jsonl").messages[0]
        assert m.is_sidechain is False
        assert m.agent_run_id is None

    def test_one_subagent_run_row_per_agent_id(self) -> None:
        lines = claude_assistant_lines(
            blocks=("text", "tool_use"), agent_id="agent-1", is_sidechain=True
        )
        result = parse_lines(_raw(lines), source_file="/x.jsonl")
        assert len(result.subagent_runs) == 1

    def test_meta_json_stores_only_the_task_digest(self) -> None:
        """`description` is the task prompt — conversation content (§8)."""
        meta = claude_meta_json(description=f"{CANARY} do the thing")
        run = parse_meta_json(
            json.dumps(meta), source_file="/a.meta.json",
            session_id="sess-a", agent_run_id="agent-1",
        )
        assert run is not None
        assert run.agent_type == "Explore"
        assert run.spawn_depth == 1
        assert run.task_sha256 is not None
        assert CANARY not in json.dumps(run.to_row("now"))

    def test_malformed_meta_json_returns_none_not_an_exception(self) -> None:
        assert parse_meta_json("{not json", source_file="/a", session_id="s", agent_run_id="a") is None


class TestTolerance:
    def test_malformed_lines_are_counted_and_skipped(self) -> None:
        """architecture.md §4.1: agents update and schemas drift; a bad line must
        never abort a file."""
        entries = _raw(claude_assistant_lines(blocks=("text",)))
        entries.insert(0, "{not json at all")
        entries.append('{"type":"assistant"')

        result = parse_lines(entries, source_file="/x.jsonl")
        assert len(result.messages) == 1
        assert result.lines_skipped == 2
        assert result.lines_read == 3

    def test_unknown_line_types_are_ignored_without_counting_as_errors(self) -> None:
        entries = _raw([
            {"type": "attachment", "sessionId": "s", "timestamp": "2026-07-01T10:00:00Z"},
            {"type": "some-future-type", "sessionId": "s", "timestamp": "2026-07-01T10:00:00Z"},
        ])
        result = parse_lines(entries, source_file="/x.jsonl")
        assert result.messages == []
        assert result.lines_skipped == 0, "a new line type is not corruption"

    def test_bookkeeping_lines_without_a_session_are_not_corruption(self) -> None:
        """`file-history-delta` / `file-history-snapshot` are real Claude Code line
        types that carry no `sessionId`. Counting them as bad lines produced
        thousands of false alarms against live data and would mask genuine drift.
        """
        entries = _raw([
            {"type": "file-history-snapshot", "messageId": "x", "snapshot": {}},
            {"type": "file-history-delta", "delta": {}},
            *claude_assistant_lines(blocks=("text",)),
        ])
        result = parse_lines(entries, source_file="/x.jsonl")

        assert len(result.messages) == 1
        assert result.lines_skipped == 0

    def test_an_assistant_line_missing_its_session_is_still_corruption(self) -> None:
        """The tolerance above must not extend to lines that should carry usage."""
        entries = _raw([
            {"type": "assistant", "timestamp": "2026-07-01T10:00:00Z",
             "message": {"id": "msg_x", "usage": {"input_tokens": 5}}},
        ])
        result = parse_lines(entries, source_file="/x.jsonl")
        assert result.messages == []
        assert result.lines_skipped == 1

    def test_assistant_line_without_message_id_is_skipped(self) -> None:
        entries = _raw([{
            "type": "assistant", "sessionId": "s", "timestamp": "2026-07-01T10:00:00Z",
            "message": {"role": "assistant", "usage": {"input_tokens": 5}},
        }])
        result = parse_lines(entries, source_file="/x.jsonl")
        assert result.messages == []
        assert result.lines_skipped == 1

    def test_json_array_line_is_skipped_not_crashed(self) -> None:
        result = parse_lines(["[1,2,3]"], source_file="/x.jsonl")
        assert result.lines_skipped == 1


class TestTimestampParsing:
    def test_z_suffix(self) -> None:
        assert parse_iso_ms("2026-07-01T10:00:00.000Z") == 1782900000000

    def test_offset_form(self) -> None:
        assert parse_iso_ms("2026-07-01T07:00:00.000-03:00") == 1782900000000

    def test_garbage_returns_none_rather_than_raising(self) -> None:
        assert parse_iso_ms("not-a-timestamp") is None
        assert parse_iso_ms(None) is None
        assert parse_iso_ms("") is None
