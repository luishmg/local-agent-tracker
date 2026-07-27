"""
`tracker pricing update` — refresh the vendored rate table (ADR-0004).

The point of vendoring is that a Collector Run is deterministic and offline-safe:
costs never depend on a network call, and history is never silently repriced.
This command is the only thing that changes rates, and it deliberately stops short
of committing — ADR-0004's "fetch -> diff -> commit" ends with a human reading the
diff.

The upstream file carries thousands of models across every provider. Keeping all
of it would make the vendored copy unreviewable, which defeats the purpose, so a
prefix allowlist trims it to the providers actually in use here.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

#: Only models under these prefixes are vendored.
MODEL_PREFIXES: tuple[str, ...] = (
    "claude-", "anthropic/", "anthropic.",
    "deepseek/", "moonshotai/", "z-ai/", "google/", "microsoft/", "openrouter/",
)

#: Upstream key -> ours. LiteLLM has renamed these before, so a missing key is
#: reported rather than silently treated as a zero rate.
_KEY_MAP: dict[str, str] = {
    "input_usd_per_token": "input_cost_per_token",
    "output_usd_per_token": "output_cost_per_token",
    "cache_read_usd_per_token": "cache_read_input_token_cost",
    "cache_write_5m_usd_per_token": "cache_creation_input_token_cost",
}

#: Tried in order for the 1h tier, which upstream has spelled several ways.
_CACHE_1H_KEYS = (
    "cache_creation_input_token_cost_above_1hr",
    "cache_creation_input_token_cost_1h",
)


@dataclass(slots=True)
class UpdateResult:
    changed: bool
    version: str
    model_count: int
    written_path: Path | None
    diff: str
    missing_rate_models: list[str]
    message: str


def _data_dir() -> Path:
    return Path(str(resources.files("tracker.pricing.data")))


def _convert(upstream: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Filter and translate the upstream table. Returns (models, models_missing_rates)."""
    models: dict[str, Any] = {}
    missing: list[str] = []

    for name, entry in upstream.items():
        if name == "sample_spec" or not isinstance(entry, dict):
            continue
        if not any(name.startswith(p) for p in MODEL_PREFIXES):
            continue

        converted: dict[str, Any] = {"upstream_key": name}
        for ours, theirs in _KEY_MAP.items():
            converted[ours] = entry.get(theirs)

        rate_1h = next(
            (entry[k] for k in _CACHE_1H_KEYS if entry.get(k) is not None), None
        )
        if rate_1h is not None:
            converted["cache_write_1h_usd_per_token"] = rate_1h
            converted["cache_write_1h_source"] = "upstream"
        elif converted.get("input_usd_per_token") is not None:
            # The published 1h multiplier is 2x input.
            converted["cache_write_1h_usd_per_token"] = 2 * converted["input_usd_per_token"]
            converted["cache_write_1h_source"] = "derived-2x-input"
        else:
            converted["cache_write_1h_usd_per_token"] = None
            converted["cache_write_1h_source"] = None

        if converted.get("input_usd_per_token") is None:
            missing.append(name)

        models[name] = converted

    return dict(sorted(models.items())), sorted(missing)


def fetch_upstream(url: str = LITELLM_URL, timeout: float = 30.0) -> tuple[dict[str, Any], str]:
    """Download the upstream table. Returns (parsed, sha256-of-raw-bytes)."""
    import httpx

    response = httpx.get(url, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    raw = response.content
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def update(
    *,
    data_dir: Path | None = None,
    url: str = LITELLM_URL,
    upstream: dict[str, Any] | None = None,
    upstream_sha256: str | None = None,
    today: str | None = None,
) -> UpdateResult:
    """Fetch, convert, write a new version, and print a diff for a human.

    `upstream` bypasses the network, which is how this is tested offline.
    """
    directory = data_dir or _data_dir()
    active_path = directory / "ACTIVE"
    active = active_path.read_text(encoding="utf-8").strip() if active_path.exists() else None

    if upstream is None:
        upstream, upstream_sha256 = fetch_upstream(url)

    previous_payload: dict[str, Any] = {}
    if active:
        previous_file = directory / f"pricing-{active}.json"
        if previous_file.exists():
            previous_payload = json.loads(previous_file.read_text(encoding="utf-8"))

    if (
        upstream_sha256 is not None
        and previous_payload.get("upstream_sha256") == upstream_sha256
    ):
        return UpdateResult(
            changed=False, version=active or "", model_count=0, written_path=None,
            diff="", missing_rate_models=[],
            message="upstream is unchanged since the active version; nothing written",
        )

    models, missing = _convert(upstream)
    version = today or datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    payload = {
        "version": version,
        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
        "source_url": url,
        "upstream_sha256": upstream_sha256,
        "models": models,
    }
    rendered = json.dumps(payload, indent=2, sort_keys=False) + "\n"

    diff = "".join(
        difflib.unified_diff(
            json.dumps(previous_payload.get("models", {}), indent=2,
                       sort_keys=True).splitlines(keepends=True),
            json.dumps(models, indent=2, sort_keys=True).splitlines(keepends=True),
            fromfile=f"pricing-{active}.json" if active else "(none)",
            tofile=f"pricing-{version}.json",
            n=2,
        )
    )

    target = directory / f"pricing-{version}.json"
    target.write_text(rendered, encoding="utf-8")
    active_path.write_text(version + "\n", encoding="utf-8")

    return UpdateResult(
        changed=True, version=version, model_count=len(models), written_path=target,
        diff=diff, missing_rate_models=missing,
        message=(
            f"wrote {target.name} ({len(models)} models) and set it active. "
            f"Review the diff, then commit -- this command deliberately does not."
        ),
    )
