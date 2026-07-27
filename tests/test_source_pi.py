"""
pi parser.

The tests that matter most here cover the three things architecture.md §3.1 does
not describe: compaction entries are billable, `usage.reasoning` is a subset of
output rather than an addend, and the compaction entries carry conversation
content that must never reach the store.
"""

from __future__ import annotations

import json

from tests.factories import (
    CANARY,
    pi_assistant_entry,
    pi_compaction_entry,
    pi_model_change,
    pi_session_header,
    pi_tool_result_entry,
)
from tracker.sources.pi import SessionState, parse_lines


def _raw(entries: list[dict]) -> list[str]:
    return [json.dumps(e) for e in entries]


SESSION_ID = "019f0000-0000-7000-8000-000000000000"


class TestReasoningTokens:
    def test_reasoning_is_stored_but_not_added_to_totals(self) -> None:
        """Verified against live data: input + output + cacheRead + cacheWrite
        equals totalTokens exactly *while* reasoning is non-zero. Treating
        reasoning as an additional charge double-bills the thinking."""
        entries = [
            pi_session_header(session_id=SESSION_ID),
            pi_assistant_entry(input_tokens=9622, output_tokens=301, reasoning=132),
        ]
        usage = parse_lines(_raw(entries), source_file="/x.jsonl").messages[0].usage

        assert usage.reasoning == 132
        assert usage.output == 301, "reasoning must not be added onto output"
        assert usage.total == 9923
        assert usage.total == usage.input + usage.output + usage.cache_read + usage.cache_write

    def test_pi_reported_total_is_trusted_over_recomputation(self) -> None:
        """pi computes totalTokens itself; the parser must not silently disagree."""
        entries = [
            pi_session_header(session_id=SESSION_ID),
            pi_assistant_entry(
                input_tokens=556, output_tokens=208, cache_read=22272,
                reasoning=69, total_tokens=23036,
            ),
        ]
        assert parse_lines(_raw(entries), source_file="/x.jsonl").messages[0].usage.total == 23036

    def test_absent_reasoning_is_zero(self) -> None:
        entries = [pi_session_header(session_id=SESSION_ID), pi_assistant_entry(reasoning=None)]
        assert parse_lines(_raw(entries), source_file="/x.jsonl").messages[0].usage.reasoning == 0


class TestCompaction:
    def test_compaction_spend_is_captured(self) -> None:
        """Undocumented in architecture.md, and real money: dropping it
        under-reports pi cost with no visible symptom."""
        entries = [
            pi_session_header(session_id=SESSION_ID),
            pi_compaction_entry(input_tokens=8000, output_tokens=1200, cost_total=0.0456),
        ]
        messages = parse_lines(_raw(entries), source_file="/x.jsonl").messages

        assert len(messages) == 1
        assert messages[0].kind == "compaction"
        assert messages[0].usage.input == 8000
        assert messages[0].reported_cost_usd == 0.0456
        assert messages[0].cost_source == "reported"

    def test_compaction_inherits_the_active_model(self) -> None:
        """A compaction entry has no `model` of its own, so without the
        model_change state machine its spend is unattributable."""
        entries = [
            pi_session_header(session_id=SESSION_ID),
            pi_model_change(model_id="z-ai/glm-4.6"),
            pi_compaction_entry(),
        ]
        assert parse_lines(_raw(entries), source_file="/x.jsonl").messages[0].model == "z-ai/glm-4.6"

    def test_compaction_inherits_a_model_set_by_an_assistant_entry(self) -> None:
        """A session need not open with an explicit model_change."""
        entries = [
            pi_session_header(session_id=SESSION_ID),
            pi_assistant_entry(entry_id="a1", model="moonshotai/kimi-k2"),
            pi_compaction_entry(entry_id="c1"),
        ]
        messages = parse_lines(_raw(entries), source_file="/x.jsonl").messages
        compaction = next(m for m in messages if m.kind == "compaction")
        assert compaction.model == "moonshotai/kimi-k2"

    def test_model_change_after_compaction_does_not_retroactively_apply(self) -> None:
        entries = [
            pi_session_header(session_id=SESSION_ID),
            pi_model_change(model_id="model-a"),
            pi_compaction_entry(entry_id="c1"),
            pi_model_change(entry_id="mc2", model_id="model-b"),
            pi_compaction_entry(entry_id="c2"),
        ]
        models = [m.model for m in parse_lines(_raw(entries), source_file="/x.jsonl").messages]
        assert models == ["model-a", "model-b"]

    def test_older_compaction_without_usage_is_still_recorded(self) -> None:
        """Compaction entries from June carry no usage block; recording them with
        zero tokens and an explicit `unknown` cost beats dropping them silently."""
        entries = [
            pi_session_header(session_id=SESSION_ID),
            pi_compaction_entry(with_usage=False),
        ]
        m = parse_lines(_raw(entries), source_file="/x.jsonl").messages[0]

        assert m.kind == "compaction"
        assert m.usage.total == 0
        assert m.cost_usd is None
        assert m.cost_source == "unknown"

    def test_tokens_before_is_kept_as_context(self) -> None:
        entries = [
            pi_session_header(session_id=SESSION_ID),
            pi_compaction_entry(tokens_before=145_000),
        ]
        assert parse_lines(_raw(entries), source_file="/x.jsonl").messages[0].context_tokens == 145_000

    def test_summary_and_file_paths_never_survive_parsing(self) -> None:
        """`summary` is a verbatim conversation summary and the file lists are the
        user's paths -- both are content under architecture.md §8."""
        entries = [
            pi_session_header(session_id=SESSION_ID),
            pi_compaction_entry(read_files=3, modified_files=1),
        ]
        m = parse_lines(_raw(entries), source_file="/x.jsonl").messages[0]

        assert CANARY not in json.dumps(m.to_row("now"))
        assert m.stop_reason == "compaction:read=3,modified=1", "only the shape survives"


class TestFieldNormalization:
    def test_pi_token_names_map_to_normalized_names(self) -> None:
        entries = [
            pi_session_header(session_id=SESSION_ID),
            pi_assistant_entry(input_tokens=10, output_tokens=20, cache_read=30, cache_write=40),
        ]
        usage = parse_lines(_raw(entries), source_file="/x.jsonl").messages[0].usage

        assert (usage.input, usage.output) == (10, 20)
        assert usage.cache_read == 30
        assert usage.cache_write == 40
        assert usage.cache_write_5m == 40, "pi's providers offer no 1h cache tier"
        assert usage.cache_write_1h == 0

    def test_reported_cost_is_used_directly(self) -> None:
        """pi writes its own cost, so no pricing-table derivation applies."""
        entries = [pi_session_header(session_id=SESSION_ID), pi_assistant_entry(cost_total=0.00123)]
        m = parse_lines(_raw(entries), source_file="/x.jsonl").messages[0]

        assert m.reported_cost_usd == 0.00123
        assert m.cost_usd == 0.00123
        assert m.cost_source == "reported"
        assert m.derived_cost_usd is None

    def test_stop_reason_camel_case_is_normalized(self) -> None:
        entries = [
            pi_session_header(session_id=SESSION_ID),
            pi_assistant_entry(stop_reason="toolUse"),
        ]
        assert parse_lines(_raw(entries), source_file="/x.jsonl").messages[0].stop_reason == "toolUse"

    def test_session_header_supplies_cwd(self) -> None:
        entries = [
            pi_session_header(session_id=SESSION_ID, cwd="/home/user/Projects/demo"),
            pi_assistant_entry(),
        ]
        assert parse_lines(_raw(entries), source_file="/x.jsonl").messages[0].cwd == (
            "/home/user/Projects/demo"
        )

    def test_provider_and_api_are_captured(self) -> None:
        entries = [
            pi_session_header(session_id=SESSION_ID),
            pi_assistant_entry(provider="openrouter", api="chat-completions"),
        ]
        m = parse_lines(_raw(entries), source_file="/x.jsonl").messages[0]
        assert m.provider == "openrouter"
        assert m.api == "chat-completions"


class TestToolActivity:
    def test_tool_calls_and_results_share_a_key(self) -> None:
        """The call and its result are separate entries that must merge onto one
        row, so both sides must produce the same dedup key."""
        entries = [
            pi_session_header(session_id=SESSION_ID),
            pi_assistant_entry(tool_calls=(("call_0001", "read"),)),
            pi_tool_result_entry(tool_call_id="call_0001", tool_name="read", is_error=True),
        ]
        calls = parse_lines(_raw(entries), source_file="/x.jsonl").tool_calls

        assert len({c.dedup_key for c in calls}) == 1
        assert any(c.tool_name == "read" for c in calls)
        assert any(c.is_error is True for c in calls)

    def test_successful_tool_result_is_not_an_error(self) -> None:
        entries = [
            pi_session_header(session_id=SESSION_ID),
            pi_tool_result_entry(tool_call_id="call_1", is_error=False),
        ]
        assert parse_lines(_raw(entries), source_file="/x.jsonl").tool_calls[0].is_error is False


class TestStreamingState:
    def test_state_survives_an_incremental_read(self) -> None:
        """A pi file is read across Collector Runs; the session header and the
        active model are established once, early, and must persist."""
        head = [pi_session_header(session_id=SESSION_ID, cwd="/w"), pi_model_change(model_id="m-1")]
        tail = [pi_compaction_entry()]
        state = SessionState()

        parse_lines(_raw(head), source_file="/x.jsonl", state=state)
        result = parse_lines(_raw(tail), source_file="/x.jsonl", state=state)

        assert result.messages[0].model == "m-1"
        assert result.messages[0].cwd == "/w"
        assert result.messages[0].session_id == SESSION_ID

    def test_missing_header_falls_back_to_the_supplied_session_id(self) -> None:
        """An incremental read can begin past the header line entirely."""
        result = parse_lines(
            _raw([pi_assistant_entry()]),
            source_file="/x.jsonl",
            fallback_session_id="from-filename",
        )
        assert result.messages[0].session_id == "from-filename"

    def test_entries_without_a_resolvable_session_are_skipped(self) -> None:
        result = parse_lines(_raw([pi_assistant_entry()]), source_file="/x.jsonl")
        assert result.messages == []
        assert result.lines_skipped == 1


class TestNestedSubagentSessions:
    def test_subagent_context_is_attached(self) -> None:
        """Nested subagent sessions live five levels deep and are real spend."""
        entries = [pi_session_header(session_id="sub-1"), pi_assistant_entry()]
        result = parse_lines(
            _raw(entries), source_file="/x.jsonl",
            parent_session_id="parent-1", agent_run_id="b2954803",
        )
        m = result.messages[0]

        assert m.parent_session_id == "parent-1"
        assert m.agent_run_id == "b2954803"
        assert m.is_sidechain is True

    def test_top_level_sessions_are_not_sidechains(self) -> None:
        entries = [pi_session_header(session_id=SESSION_ID), pi_assistant_entry()]
        assert parse_lines(_raw(entries), source_file="/x.jsonl").messages[0].is_sidechain is False


class TestTolerance:
    def test_malformed_lines_are_counted_and_skipped(self) -> None:
        entries = _raw([pi_session_header(session_id=SESSION_ID), pi_assistant_entry()])
        entries.insert(1, "{broken json")

        result = parse_lines(entries, source_file="/x.jsonl")
        assert len(result.messages) == 1
        assert result.lines_skipped == 1

    def test_unknown_entry_types_are_ignored(self) -> None:
        entries = _raw([
            pi_session_header(session_id=SESSION_ID),
            {"type": "some-new-pi-entry", "id": "x", "timestamp": "2026-07-01T10:00:00.000Z"},
        ])
        result = parse_lines(entries, source_file="/x.jsonl")
        assert result.messages == []
        assert result.lines_skipped == 0

    def test_thinking_level_change_is_ignored_cleanly(self) -> None:
        entries = _raw([
            pi_session_header(session_id=SESSION_ID),
            {"type": "thinking_level_change", "id": "t1", "timestamp": "2026-07-01T10:00:00.000Z"},
        ])
        assert parse_lines(entries, source_file="/x.jsonl").lines_skipped == 0

    def test_assistant_entry_without_usage_is_not_billable(self) -> None:
        entry = pi_assistant_entry()
        del entry["message"]["usage"]
        entries = _raw([pi_session_header(session_id=SESSION_ID), entry])

        assert parse_lines(entries, source_file="/x.jsonl").messages == []

    def test_user_turns_carry_no_usage(self) -> None:
        entries = _raw([
            pi_session_header(session_id=SESSION_ID),
            {"type": "message", "id": "u1", "timestamp": "2026-07-01T10:00:00.000Z",
             "message": {"role": "user", "content": [{"type": "text", "text": CANARY}]}},
        ])
        assert parse_lines(entries, source_file="/x.jsonl").messages == []
