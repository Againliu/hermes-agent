"""Behaviour contracts: user ``pricing_overrides``."""

from decimal import Decimal

import json

import pytest

from agent.usage_pricing import estimate_usage_cost, normalize_usage
from hermes_constants import get_hermes_home


def _write_config(data: dict) -> None:
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(json.dumps(data), encoding="utf-8")  # JSON is valid YAML


@pytest.mark.parametrize("entry, model, provider, base_url", [
    # A custom gateway model: no snapshot, no /models pricing.
    ({"provider": "custom", "model": "relay-model-1"}, "relay-model-1", "custom", "http://127.0.0.1:9/v1"),
    # ``custom:<name>`` spelling against the canonical route; full id vs basename.
    ({"provider": "custom:fireworks", "model": "kimi-k2p6"},
     "accounts/fireworks/models/kimi-k2p6", "custom:fireworks", "https://api.fireworks.ai/inference/v1"),
    # No provider: matches every route, and wins over the bundled snapshot.
    ({"model": "claude-opus-5"}, "claude-opus-5", "anthropic", None),
])
def test_pricing_overrides_in_config_price_the_route(entry, model, provider, base_url):
    _write_config({"pricing_overrides": [
        {**entry, "output_per_million": 9},  # missing a required rate: skipped, never masks the next
        {**entry, "input_per_million": 1, "output_per_million": 3, "cache_write_per_million": 4},
    ]})
    usage = normalize_usage({
        "input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000,
    }, provider="anthropic", api_mode="anthropic_messages")

    result = estimate_usage_cost(model, usage, provider=provider, base_url=base_url)

    assert (result.source, result.status, result.amount_usd) == ("user_override", "estimated", Decimal(8))

