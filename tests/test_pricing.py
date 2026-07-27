"""
Cost derivation.

The two tests worth writing first are `unknown_model_is_null_never_zero` and
`synthetic_is_zero_rated_not_unknown`: they pin the distinction between "we don't
know what this cost" and "this cost nothing", which is the whole point of ADR-0004
and the difference between a cheap-looking day and an incomplete one.

All of this runs offline against the vendored table.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracker.normalize.models import TokenUsage
from tracker.pricing import ModelRates, PricingTable, load_pricing_table

MTOK = 1_000_000


@pytest.fixture(scope="module")
def table() -> PricingTable:
    return load_pricing_table()


@pytest.fixture
def tiny_table(tmp_path: Path) -> PricingTable:
    """Round numbers, so expected costs are checkable by hand."""
    (tmp_path / "ACTIVE").write_text("test-1", encoding="utf-8")
    (tmp_path / "pricing-test-1.json").write_text(
        json.dumps({
            "version": "test-1",
            "models": {
                "round-model": {
                    "input_usd_per_token": 1e-06,
                    "output_usd_per_token": 1e-05,
                    "cache_read_usd_per_token": 1e-07,
                    "cache_write_5m_usd_per_token": 2e-06,
                    "cache_write_1h_usd_per_token": 4e-06,
                },
            },
        }),
        encoding="utf-8",
    )
    (tmp_path / "model_aliases.json").write_text(
        json.dumps({"_comment": "doc", "alias-model": "round-model", "<synthetic>": None}),
        encoding="utf-8",
    )
    return load_pricing_table(data_dir=tmp_path)


class TestCostMath:
    def test_each_token_class_is_priced_at_its_own_rate(self, tiny_table: PricingTable) -> None:
        usage = TokenUsage(
            input=1000, output=100, cache_read=10_000,
            cache_write_5m=500, cache_write_1h=250,
        )
        result = tiny_table.derive_cost("round-model", usage)

        expected = (
            1000 * 1e-06      # 0.001
            + 100 * 1e-05     # 0.001
            + 10_000 * 1e-07  # 0.001
            + 500 * 2e-06     # 0.001
            + 250 * 4e-06     # 0.001
        )
        assert result.cost_usd == pytest.approx(expected)
        assert result.cost_usd == pytest.approx(0.005)
        assert result.source == "derived"
        assert result.pricing_version == "test-1"

    def test_5m_and_1h_cache_writes_are_priced_separately(self, tiny_table: PricingTable) -> None:
        """architecture.md §3.2 -- merging them misprices any 1h-cache turn."""
        only_5m = tiny_table.derive_cost("round-model", TokenUsage(cache_write_5m=1000))
        only_1h = tiny_table.derive_cost("round-model", TokenUsage(cache_write_1h=1000))

        assert only_5m.cost_usd == pytest.approx(0.002)
        assert only_1h.cost_usd == pytest.approx(0.004)
        assert only_1h.cost_usd != only_5m.cost_usd

    def test_reasoning_tokens_are_not_a_cost_term(self, tiny_table: PricingTable) -> None:
        """pi's `reasoning` is a subset of `output`; charging for it double-bills
        the thinking."""
        without = tiny_table.derive_cost("round-model", TokenUsage(input=100, output=50))
        with_reasoning = tiny_table.derive_cost(
            "round-model", TokenUsage(input=100, output=50, reasoning=40)
        )
        assert with_reasoning.cost_usd == pytest.approx(without.cost_usd)

    def test_zero_usage_costs_zero_not_none(self, tiny_table: PricingTable) -> None:
        result = tiny_table.derive_cost("round-model", TokenUsage())
        assert result.cost_usd == 0.0
        assert result.source == "derived"


class TestResolution:
    def test_unknown_model_is_null_never_zero(self, tiny_table: PricingTable) -> None:
        """The failure ADR-0004 exists to prevent: a silent zero makes a partly
        unpriced day look cheap rather than incomplete."""
        result = tiny_table.derive_cost("some-model-we-never-saw", TokenUsage(input=999_999))

        assert result.cost_usd is None
        assert result.cost_usd != 0.0
        assert result.source == "unknown"
        assert result.pricing_version is None
        assert result.unknown_model == "some-model-we-never-saw"

    def test_synthetic_is_zero_rated_not_unknown(self, tiny_table: PricingTable) -> None:
        """`<synthetic>` responses are local and unbilled -- an explicit zero, and
        crucially not something that trips the missing-pricing warning."""
        result = tiny_table.derive_cost("<synthetic>", TokenUsage(input=5000, output=5000))

        assert result.cost_usd == 0.0
        assert result.source == "zero-rated"
        assert result.unknown_model is None

    def test_alias_resolves_to_its_target_rates(self, tiny_table: PricingTable) -> None:
        direct = tiny_table.derive_cost("round-model", TokenUsage(input=1000))
        aliased = tiny_table.derive_cost("alias-model", TokenUsage(input=1000))
        assert aliased.cost_usd == pytest.approx(direct.cost_usd)

    def test_trailing_date_suffix_is_stripped(self, table: PricingTable) -> None:
        """`claude-haiku-4-5-20251001` appears in real transcripts; the table keys
        the undated id."""
        assert table.resolve("claude-haiku-4-5-20251001") is not None
        assert table.resolve("claude-haiku-4-5-20251001") == table.resolve("claude-haiku-4-5")

    def test_missing_model_id_is_unknown(self, tiny_table: PricingTable) -> None:
        assert tiny_table.derive_cost(None, TokenUsage()).source == "unknown"

    def test_doc_keys_in_the_alias_file_are_not_aliases(self, tiny_table: PricingTable) -> None:
        assert "_comment" not in tiny_table.aliases()


class TestVendoredTable:
    """The shipped table must actually cover what this laptop runs."""

    @pytest.mark.parametrize(
        "model",
        ["claude-opus-4-8", "claude-fable-5", "claude-opus-5",
         "claude-haiku-4-5-20251001", "claude-sonnet-5"],
    )
    def test_every_observed_claude_model_is_priced(self, table: PricingTable, model: str) -> None:
        """These five account for essentially all Claude Code volume here; a miss
        means real spend prices to NULL."""
        assert table.resolve(model) is not None, f"{model} would price as unknown"

    def test_synthetic_is_configured_zero_rated(self, table: PricingTable) -> None:
        assert table.resolve("<synthetic>") == "zero-rated"

    def test_fable_is_priced_directly_not_aliased_to_opus(self, table: PricingTable) -> None:
        """claude-fable-5 is a real $10/$50 model. Aliasing it to an Opus-tier key
        would halve its reported cost."""
        assert "claude-fable-5" not in table.aliases()
        fable = table.resolve("claude-fable-5")
        opus = table.resolve("claude-opus-4-8")
        assert isinstance(fable, ModelRates) and isinstance(opus, ModelRates)
        assert fable.input_usd_per_token == pytest.approx(2 * (opus.input_usd_per_token or 0))

    @pytest.mark.parametrize(
        ("model", "input_per_mtok", "output_per_mtok"),
        [
            ("claude-opus-5", 5.0, 25.0),
            ("claude-opus-4-8", 5.0, 25.0),
            ("claude-fable-5", 10.0, 50.0),
            ("claude-sonnet-5", 2.0, 10.0),   # introductory rate, through 2026-08-31
            ("claude-haiku-4-5", 1.0, 5.0),
        ],
    )
    def test_published_per_mtok_rates(
        self, table: PricingTable, model: str, input_per_mtok: float, output_per_mtok: float
    ) -> None:
        rates = table.resolve(model)
        assert isinstance(rates, ModelRates)
        assert (rates.input_usd_per_token or 0) * MTOK == pytest.approx(input_per_mtok)
        assert (rates.output_usd_per_token or 0) * MTOK == pytest.approx(output_per_mtok)

    @pytest.mark.parametrize(
        "model",
        ["claude-opus-5", "claude-opus-4-8", "claude-fable-5",
         "claude-sonnet-5", "claude-haiku-4-5"],
    )
    def test_cache_rates_follow_the_documented_multipliers(
        self, table: PricingTable, model: str
    ) -> None:
        """Cache read is 0.1x input, 5m write 1.25x, 1h write 2x."""
        r = table.resolve(model)
        assert isinstance(r, ModelRates)
        base = r.input_usd_per_token or 0
        assert (r.cache_read_usd_per_token or 0) == pytest.approx(base * 0.1)
        assert (r.cache_write_5m_usd_per_token or 0) == pytest.approx(base * 1.25)
        assert (r.cache_write_1h_usd_per_token or 0) == pytest.approx(base * 2.0)

    def test_sonnet_5_intro_pricing_is_documented_as_expiring(self, table: PricingTable) -> None:
        """The introductory rate lapses on 2026-08-31; without a note, the next
        person to read this file has no way to know the number is time-bound."""
        entry = table.models()["claude-sonnet-5"]
        assert entry.input_usd_per_token == pytest.approx(2e-06)
        raw = json.loads(
            (Path(__file__).parents[1] / "tracker" / "pricing" / "data"
             / f"pricing-{table.version}.json").read_text(encoding="utf-8")
        )
        assert "2026-08-31" in raw["models"]["claude-sonnet-5"]["note"]


class TestPriceMessage:
    def test_pi_reported_cost_wins_over_any_table(self, table: PricingTable) -> None:
        """pi computed its cost against the rates actually billed."""
        result = table.price_message(
            agent="pi", model="deepseek/deepseek-v4-flash",
            usage=TokenUsage(input=9622, output=301), reported_cost_usd=0.00123,
        )
        assert result.cost_usd == 0.00123
        assert result.source == "reported"

    def test_claude_message_is_derived(self, table: PricingTable) -> None:
        result = table.price_message(
            agent="claude-code", model="claude-opus-4-8",
            usage=TokenUsage(input=1_000_000), reported_cost_usd=None,
        )
        assert result.source == "derived"
        assert result.cost_usd == pytest.approx(5.0)

    def test_pi_without_reported_cost_is_unknown_not_derived(self, table: PricingTable) -> None:
        """Covering a dozen OpenRouter models is a project in itself; flag rather
        than guess (increment 1 scope)."""
        result = table.price_message(
            agent="pi", model="z-ai/glm-5.2",
            usage=TokenUsage(input=1000), reported_cost_usd=None,
        )
        assert result.cost_usd is None
        assert result.source == "unknown"


def test_table_loads_from_the_installed_package(table: PricingTable) -> None:
    """Pricing lives inside the package so it ships with any install."""
    assert table.version == "2026-07-26"
    assert table.model_count >= 8
