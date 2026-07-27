"""
The vendored pricing table and cost derivation (ADR-0004).

Claude-only by design. pi writes `usage.cost` into every message it produces, so
its Reported Cost is authoritative and needs no table lookup; Claude Code writes
no cost field at all, so its cost must be Derived from token counts.

Three rules the rest of the system depends on:

**An unknown model prices to NULL, never to zero.** A zero silently makes an
unpriced day look cheap instead of incomplete, which is the exact failure ADR-0004
exists to prevent. Unknown model ids are collected per run and reported once.

**`<synthetic>` is a fourth state, not an unknown.** Those responses are generated
locally and never billed, so they resolve to an explicit 0.0 with
`cost_source='zero-rated'` and must not trip the missing-pricing warning.

**Reasoning tokens are never a cost term.** pi's `usage.reasoning` is a subset of
`output`, so adding it would double-bill the thinking. It is not referenced here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from tracker.normalize.models import TokenUsage

CostSource = Literal["reported", "derived", "zero-rated", "unknown"]

#: A trailing release date on a model id (`claude-haiku-4-5-20251001`).
_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")

_DATA_PACKAGE = "tracker.pricing.data"


@dataclass(slots=True, frozen=True)
class ModelRates:
    """Per-token rates for one model. `None` means the rate is unpublished."""

    model: str
    input_usd_per_token: float | None = None
    output_usd_per_token: float | None = None
    cache_read_usd_per_token: float | None = None
    cache_write_5m_usd_per_token: float | None = None
    cache_write_1h_usd_per_token: float | None = None
    cache_write_1h_source: str | None = None
    upstream_key: str | None = None


@dataclass(slots=True, frozen=True)
class CostResult:
    """The outcome of pricing one Message."""

    cost_usd: float | None
    source: CostSource
    pricing_version: str | None
    #: The model id that could not be priced, when `source == 'unknown'`.
    unknown_model: str | None = None


class PricingTable:
    """A loaded pricing version plus its alias map. Never raises on lookup."""

    __slots__ = ("version", "_models", "_aliases", "_fetched_at", "_source_url")

    def __init__(
        self,
        version: str,
        models: dict[str, ModelRates],
        aliases: dict[str, str | None],
        fetched_at: str | None = None,
        source_url: str | None = None,
    ) -> None:
        self.version = version
        self._models = models
        self._aliases = aliases
        self._fetched_at = fetched_at
        self._source_url = source_url

    # -- introspection ----------------------------------------------------- #

    @property
    def model_count(self) -> int:
        return len(self._models)

    @property
    def alias_count(self) -> int:
        return len(self._aliases)

    @property
    def fetched_at(self) -> str | None:
        return self._fetched_at

    @property
    def source_url(self) -> str | None:
        return self._source_url

    def models(self) -> dict[str, ModelRates]:
        return dict(self._models)

    def aliases(self) -> dict[str, str | None]:
        return dict(self._aliases)

    # -- resolution -------------------------------------------------------- #

    def resolve(self, model: str | None) -> ModelRates | None | Literal["zero-rated"]:
        """Map a model id to its rates.

        Returns `ModelRates` on a hit, the string `"zero-rated"` for a model the
        alias map deliberately maps to null, and `None` when the model is genuinely
        unknown. The three outcomes are distinct on purpose -- collapsing
        zero-rated into unknown produces spurious missing-pricing warnings, and
        collapsing unknown into zero-rated silently under-reports cost.
        """
        if not model:
            return None

        if model in self._aliases:
            target = self._aliases[model]
            if target is None:
                return "zero-rated"
            return self._models.get(target)

        rates = self._models.get(model)
        if rates is not None:
            return rates

        # `claude-haiku-4-5-20251001` -> `claude-haiku-4-5`
        stripped = _DATE_SUFFIX_RE.sub("", model)
        if stripped != model:
            return self._models.get(stripped)

        return None

    # -- costing ----------------------------------------------------------- #

    def derive_cost(self, model: str | None, usage: TokenUsage) -> CostResult:
        """Compute Derived Cost for a Claude Message.

        The 5m and 1h cache-write tiers are priced separately (architecture.md
        §3.2); merging them would misprice any turn using the 1h cache.
        """
        resolved = self.resolve(model)

        if resolved == "zero-rated":
            return CostResult(0.0, "zero-rated", self.version)
        if resolved is None:
            return CostResult(None, "unknown", None, unknown_model=model or "<missing>")

        rates: ModelRates = resolved
        total = (
            usage.input * (rates.input_usd_per_token or 0.0)
            # `usage.reasoning` is inside `output` -- deliberately not a term here.
            + usage.output * (rates.output_usd_per_token or 0.0)
            + usage.cache_read * (rates.cache_read_usd_per_token or 0.0)
            + usage.cache_write_5m * (rates.cache_write_5m_usd_per_token or 0.0)
            + usage.cache_write_1h * (rates.cache_write_1h_usd_per_token or 0.0)
        )
        return CostResult(total, "derived", self.version)

    def price_message(
        self,
        *,
        agent: str,
        model: str | None,
        usage: TokenUsage,
        reported_cost_usd: float | None,
    ) -> CostResult:
        """Decide a Message's cost.

        A Reported Cost always wins: pi computed it against the rates actually
        billed, which beats anything this table can reconstruct.
        """
        if reported_cost_usd is not None:
            return CostResult(reported_cost_usd, "reported", None)
        if agent != "claude-code":
            # pi without a reported cost: deriving would need per-model coverage
            # of a dozen OpenRouter models. Flag it rather than guess.
            return CostResult(None, "unknown", None, unknown_model=model or "<missing>")
        return self.derive_cost(model, usage)


def _parse_models(raw: dict[str, Any]) -> dict[str, ModelRates]:
    out: dict[str, ModelRates] = {}
    for name, entry in (raw or {}).items():
        if not isinstance(entry, dict):
            continue
        out[name] = ModelRates(
            model=name,
            input_usd_per_token=entry.get("input_usd_per_token"),
            output_usd_per_token=entry.get("output_usd_per_token"),
            cache_read_usd_per_token=entry.get("cache_read_usd_per_token"),
            cache_write_5m_usd_per_token=entry.get("cache_write_5m_usd_per_token"),
            cache_write_1h_usd_per_token=entry.get("cache_write_1h_usd_per_token"),
            cache_write_1h_source=entry.get("cache_write_1h_source"),
            upstream_key=entry.get("upstream_key"),
        )
    return out


def _parse_aliases(raw: dict[str, Any]) -> dict[str, str | None]:
    """Keys starting with `_` are documentation, not aliases."""
    return {
        key: value
        for key, value in (raw or {}).items()
        if not key.startswith("_") and (value is None or isinstance(value, str))
    }


def load_pricing_table(data_dir: Path | None = None) -> PricingTable:
    """Load the active pricing version.

    Reads from the installed package by default (`importlib.resources`), so the
    table travels with the code; `data_dir` overrides that for tests.
    """
    if data_dir is not None:
        active = (data_dir / "ACTIVE").read_text(encoding="utf-8").strip()
        payload = json.loads((data_dir / f"pricing-{active}.json").read_text(encoding="utf-8"))
        aliases_path = data_dir / "model_aliases.json"
        aliases_raw = (
            json.loads(aliases_path.read_text(encoding="utf-8")) if aliases_path.exists() else {}
        )
    else:
        files = resources.files(_DATA_PACKAGE)
        active = (files / "ACTIVE").read_text(encoding="utf-8").strip()
        payload = json.loads((files / f"pricing-{active}.json").read_text(encoding="utf-8"))
        aliases_raw = json.loads((files / "model_aliases.json").read_text(encoding="utf-8"))

    return PricingTable(
        version=payload.get("version", active),
        models=_parse_models(payload.get("models", {})),
        aliases=_parse_aliases(aliases_raw),
        fetched_at=payload.get("fetched_at"),
        source_url=payload.get("source_url"),
    )


@lru_cache
def get_pricing_table() -> PricingTable:
    """Process-wide cached table. Tests that need a custom table call
    `load_pricing_table(data_dir=...)` directly rather than clearing this."""
    return load_pricing_table()
