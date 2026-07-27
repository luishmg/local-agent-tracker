"""Experiment windows, the shipper seam, and `pricing update` (offline)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.factories import claude_assistant_lines, claude_session_file, claude_usage
from tracker.config import Settings, get_settings
from tracker.db.schema import SHIPPABLE_TABLES
from tracker.db.store import open_db
from tracker.experiments import ExperimentError, list_all, report, start, stop
from tracker.ingest.pipeline import collect
from tracker.pricing import get_pricing_table


@pytest.fixture
def conn():
    with open_db(get_settings().database_path) as c:
        yield c


class TestExperimentLifecycle:
    def test_start_then_stop(self, conn) -> None:
        opened = start(conn, "kimi-cache-tuning", note="trying a 1h cache TTL")
        assert opened["note"] == "trying a 1h cache TTL"

        closed = stop(conn, "kimi-cache-tuning")
        assert closed["ended_at"] > closed["started_at"]

    def test_stop_without_a_name_closes_the_only_open_one(self, conn) -> None:
        start(conn, "solo")
        assert stop(conn)["name"] == "solo"

    def test_stop_without_a_name_refuses_when_several_are_open(self, conn) -> None:
        """Overlapping windows are allowed (ADR-0003), so this must not guess."""
        start(conn, "a")
        start(conn, "b")
        with pytest.raises(ExperimentError, match="name the one to stop"):
            stop(conn)

    def test_reopening_the_same_name_is_refused(self, conn) -> None:
        """Reuse would merge two different harness states under one label."""
        start(conn, "dup")
        with pytest.raises(ExperimentError, match="already open"):
            start(conn, "dup")

        stop(conn, "dup")
        with pytest.raises(ExperimentError, match="already ran"):
            start(conn, "dup")

    def test_stopping_an_unknown_experiment_is_an_error(self, conn) -> None:
        with pytest.raises(ExperimentError, match="no experiment named"):
            stop(conn, "never-existed")

    def test_an_empty_name_is_refused(self, conn) -> None:
        with pytest.raises(ExperimentError, match="needs a name"):
            start(conn, "   ")

    def test_overlapping_windows_are_allowed(self, conn) -> None:
        start(conn, "outer")
        start(conn, "inner")
        stop(conn, "inner")
        assert len(list_all(conn)) == 2


class TestExperimentAttribution:
    def test_messages_inside_the_window_are_attributed(self, conn) -> None:
        settings: Settings = get_settings()
        start(conn, "window")

        claude_session_file(
            settings.claude_projects_dir, session_id="sess-exp",
            entries=claude_assistant_lines(
                msg_id="msg_in", session_id="sess-exp", blocks=("text",),
                model="claude-opus-4-8",
                # Inside the window: the fixture default is 2026-07-01, so the
                # experiment must be back-dated for the join to see it.
                usage=claude_usage(input_tokens=1_000_000, output_tokens=0),
            ),
        )
        conn.execute(
            "UPDATE experiments SET started_at = '2026-06-01T00:00:00Z' WHERE name = 'window'"
        )
        collect(conn, settings=settings, pricing=get_pricing_table())

        rows = report(conn, "window")
        assert rows
        assert rows[0]["model"] == "claude-opus-4-8"
        assert rows[0]["cost_usd"] == pytest.approx(5.0)

    def test_messages_outside_the_window_are_excluded(self, conn) -> None:
        settings: Settings = get_settings()
        claude_session_file(
            settings.claude_projects_dir, session_id="sess-out",
            entries=claude_assistant_lines(msg_id="msg_out", session_id="sess-out",
                                           blocks=("text",)),
        )
        collect(conn, settings=settings, pricing=get_pricing_table())

        start(conn, "later")  # opens now, well after the 2026-07-01 fixture
        assert report(conn, "later") == []

    def test_report_on_an_unknown_experiment_errors(self, conn) -> None:
        with pytest.raises(ExperimentError, match="no experiment named"):
            report(conn, "nope")


class TestShipperSeam:
    def test_every_shippable_table_starts_unshipped(self, conn) -> None:
        from tracker.ship import pending_counts

        counts = pending_counts(conn)
        assert set(counts) == set(SHIPPABLE_TABLES)

    def test_ship_is_an_explicit_not_implemented(self, conn) -> None:
        """A stub that silently succeeded would look like a working shipper."""
        from tracker.ship import ship

        with pytest.raises(NotImplementedError, match="deferred"):
            ship(conn)

    def test_pending_counts_track_ingested_rows(self, conn) -> None:
        from tracker.ship import pending_counts

        settings: Settings = get_settings()
        claude_session_file(settings.claude_projects_dir, session_id="sess-ship")
        collect(conn, settings=settings, pricing=get_pricing_table())

        assert pending_counts(conn)["messages"] > 0


class TestPricingUpdate:
    """Offline: the upstream payload is injected rather than fetched."""

    @pytest.fixture
    def data_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "pricing-data"
        d.mkdir()
        (d / "ACTIVE").write_text("2026-01-01", encoding="utf-8")
        (d / "pricing-2026-01-01.json").write_text(
            json.dumps({
                "version": "2026-01-01",
                "upstream_sha256": "old-sha",
                "models": {"claude-opus-4-8": {"input_usd_per_token": 4e-06}},
            }),
            encoding="utf-8",
        )
        return d

    def test_writes_a_new_version_and_activates_it(self, data_dir: Path) -> None:
        from tracker.pricing.litellm import update

        result = update(
            data_dir=data_dir,
            upstream={
                "claude-opus-4-8": {
                    "input_cost_per_token": 5e-06, "output_cost_per_token": 2.5e-05,
                    "cache_read_input_token_cost": 5e-07,
                    "cache_creation_input_token_cost": 6.25e-06,
                },
            },
            upstream_sha256="new-sha",
            today="2026-08-01",
        )

        assert result.changed
        assert result.version == "2026-08-01"
        assert (data_dir / "ACTIVE").read_text(encoding="utf-8").strip() == "2026-08-01"
        assert (data_dir / "pricing-2026-08-01.json").exists()

    def test_unchanged_upstream_writes_nothing(self, data_dir: Path) -> None:
        """Collection stays deterministic; a no-op update must not churn the file."""
        from tracker.pricing.litellm import update

        result = update(data_dir=data_dir, upstream={}, upstream_sha256="old-sha")

        assert not result.changed
        assert result.written_path is None
        assert (data_dir / "ACTIVE").read_text(encoding="utf-8").strip() == "2026-01-01"

    def test_only_allowlisted_providers_are_vendored(self, data_dir: Path) -> None:
        """The full upstream file is unreviewable; the whole point of vendoring is
        that a human can read the diff."""
        from tracker.pricing.litellm import update

        result = update(
            data_dir=data_dir,
            upstream={
                "claude-opus-4-8": {"input_cost_per_token": 5e-06},
                "deepseek/deepseek-v4-pro": {"input_cost_per_token": 1e-07},
                "some-other-vendor/enormous-model": {"input_cost_per_token": 9e-06},
                "sample_spec": {"input_cost_per_token": 0},
            },
            upstream_sha256="new-sha", today="2026-08-01",
        )
        written = json.loads((data_dir / "pricing-2026-08-01.json").read_text(encoding="utf-8"))

        assert set(written["models"]) == {"claude-opus-4-8", "deepseek/deepseek-v4-pro"}
        assert result.model_count == 2

    def test_missing_1h_rate_falls_back_to_2x_input_and_says_so(self, data_dir: Path) -> None:
        from tracker.pricing.litellm import update

        update(
            data_dir=data_dir,
            upstream={"claude-opus-4-8": {"input_cost_per_token": 5e-06}},
            upstream_sha256="new-sha", today="2026-08-01",
        )
        entry = json.loads(
            (data_dir / "pricing-2026-08-01.json").read_text(encoding="utf-8")
        )["models"]["claude-opus-4-8"]

        assert entry["cache_write_1h_usd_per_token"] == pytest.approx(1e-05)
        assert entry["cache_write_1h_source"] == "derived-2x-input"

    def test_upstream_1h_rate_is_preferred_over_the_derivation(self, data_dir: Path) -> None:
        from tracker.pricing.litellm import update

        update(
            data_dir=data_dir,
            upstream={"claude-opus-4-8": {
                "input_cost_per_token": 5e-06,
                "cache_creation_input_token_cost_above_1hr": 9.99e-06,
            }},
            upstream_sha256="new-sha", today="2026-08-01",
        )
        entry = json.loads(
            (data_dir / "pricing-2026-08-01.json").read_text(encoding="utf-8")
        )["models"]["claude-opus-4-8"]

        assert entry["cache_write_1h_usd_per_token"] == pytest.approx(9.99e-06)
        assert entry["cache_write_1h_source"] == "upstream"

    def test_a_model_with_no_input_rate_is_reported_not_silently_zeroed(
        self, data_dir: Path
    ) -> None:
        """LiteLLM has renamed rate keys before; a silent zero would price real
        spend at nothing."""
        from tracker.pricing.litellm import update

        result = update(
            data_dir=data_dir,
            upstream={"claude-opus-4-8": {"output_cost_per_token": 2.5e-05}},
            upstream_sha256="new-sha", today="2026-08-01",
        )
        assert result.missing_rate_models == ["claude-opus-4-8"]

    def test_a_diff_is_produced_for_review(self, data_dir: Path) -> None:
        """ADR-0004's 'fetch -> diff -> commit' ends with a human reading it."""
        from tracker.pricing.litellm import update

        result = update(
            data_dir=data_dir,
            upstream={"claude-opus-4-8": {"input_cost_per_token": 5e-06}},
            upstream_sha256="new-sha", today="2026-08-01",
        )
        assert "4e-06" in result.diff or "5e-06" in result.diff

    def test_the_alias_file_is_never_touched(self, data_dir: Path) -> None:
        """It is hand-maintained; an update overwriting it would silently drop the
        zero-rated `<synthetic>` mapping."""
        from tracker.pricing.litellm import update

        aliases = data_dir / "model_aliases.json"
        aliases.write_text(json.dumps({"<synthetic>": None}), encoding="utf-8")
        before = aliases.read_text(encoding="utf-8")

        update(
            data_dir=data_dir,
            upstream={"claude-opus-4-8": {"input_cost_per_token": 5e-06}},
            upstream_sha256="new-sha", today="2026-08-01",
        )
        assert aliases.read_text(encoding="utf-8") == before
