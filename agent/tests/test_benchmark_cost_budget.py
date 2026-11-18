"""Formal evaluation has a separately bounded currency cost."""

from decimal import Decimal
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "eval"))

from forgepilot_eval.cost_budget import assess_cost


def test_formal_cost_uses_balance_and_most_expensive_model_without_free_credit():
    result = assess_cost(token_cap=1_600_000, authorized_rmb="50", available_rmb="49",
                         required_model_ids={"task", "review"},
                         model_rates_rmb_per_million={"task": "21.6", "review": "30"},
                         non_token_max_rmb="0")
    assert result.worst_case_rmb == Decimal("48.0")
    assert result.effective_limit_rmb == Decimal("49")
    assert result.allowed
    assert not assess_cost(token_cap=1_600_000, authorized_rmb="50", available_rmb="49",
                           required_model_ids={"task", "review"},
                           model_rates_rmb_per_million={"task": "21.6", "review": "31"},
                           non_token_max_rmb="0").allowed


@pytest.mark.parametrize("rates,extra", [({}, "0"), ({"task": "NaN"}, "0"),
                                          ({"task": "-1"}, "0"), ({"task": "1"}, None)])
def test_unknown_or_unbounded_price_cannot_pass(rates, extra):
    with pytest.raises(ValueError):
        assess_cost(token_cap=1_600_000, authorized_rmb="50", available_rmb="49",
                    required_model_ids={"task"},
                    model_rates_rmb_per_million=rates, non_token_max_rmb=extra)


def test_unpriced_fallback_or_review_route_cannot_pass():
    with pytest.raises(ValueError, match="exact price bound"):
        assess_cost(token_cap=1_600_000, authorized_rmb="50", available_rmb="49",
                    required_model_ids={"task", "review"},
                    model_rates_rmb_per_million={"task": "1"}, non_token_max_rmb="0")
