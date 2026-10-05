from types import SimpleNamespace

import pytest

from deskhand.models import DEFAULT_MODEL, MODELS, ModelAlias, Usage


def usage(
    fresh: int | None = 0, out: int | None = 0, written: int | None = 0, read: int | None = 0
):
    return SimpleNamespace(
        input_tokens=fresh,
        output_tokens=out,
        cache_creation_input_tokens=written,
        cache_read_input_tokens=read,
    )


def test_each_token_kind_is_priced_at_its_own_rate():
    total = Usage()
    cost = total.add(usage(1_000_000, 1_000_000, 1_000_000, 1_000_000), MODELS[ModelAlias.opus])
    assert cost == pytest.approx(4.00 + 20.00 + 5.00 + 0.20)
    assert total.cost_usd == pytest.approx(cost)


def test_totals_accumulate_across_turns():
    total = Usage()
    spec = MODELS[ModelAlias.sonnet]
    total.add(usage(fresh=100, out=10), spec)
    total.add(usage(read=5_000, written=200, out=30), spec)
    assert (total.input_tokens, total.output_tokens) == (100, 40)
    assert (total.cache_read_tokens, total.cache_write_tokens) == (5_000, 200)


def test_missing_usage_fields_count_as_zero():
    total = Usage()
    assert total.add(usage(None, None, None, None), MODELS[DEFAULT_MODEL]) == 0


def test_every_alias_has_a_model():
    assert set(MODELS) == set(ModelAlias)
    assert MODELS[DEFAULT_MODEL].id == "claude-opus-5-5"
