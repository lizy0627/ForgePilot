"""Conservative RMB admission check for a token-limited formal campaign.

Rates supplied here must be the highest applicable rate for each model across
input/output, context-length tiers and cache states.  Free credits are ignored.
An unknown rate or unbounded non-token charge cannot pass this check.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


def _money(value, name):
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{name} must be a finite non-negative RMB amount") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{name} must be a finite non-negative RMB amount")
    return amount


@dataclass(frozen=True)
class CostAssessment:
    token_cap: int
    authorized_rmb: Decimal
    available_rmb: Decimal
    highest_model_rmb_per_million: Decimal
    non_token_max_rmb: Decimal
    worst_case_rmb: Decimal
    effective_limit_rmb: Decimal
    allowed: bool


def assess_cost(*, token_cap, authorized_rmb, available_rmb,
                required_model_ids, model_rates_rmb_per_million, non_token_max_rmb):
    """Bound expense before the first model request, including every route.

    ``model_rates_rmb_per_million`` maps *all* primary and fallback model IDs
    to their highest applicable RMB price per million tokens.  This is a
    deliberately pessimistic calculation: every token could be output by the
    most expensive configured model.  The caller must independently verify
    price provenance and the current provider balance.
    """
    if type(token_cap) is not int or token_cap <= 0:
        raise ValueError("token_cap must be a positive integer")
    if not isinstance(model_rates_rmb_per_million, dict) or not model_rates_rmb_per_million:
        raise ValueError("Every model route requires a price bound")
    if (not isinstance(required_model_ids, (set, frozenset, tuple, list))
            or not required_model_ids or any(not isinstance(item, str) or not item.strip()
                                              for item in required_model_ids)):
        raise ValueError("Configured model identifiers are required")
    if set(required_model_ids) != set(model_rates_rmb_per_million):
        raise ValueError("Every configured model route needs an exact price bound")
    rates = {}
    for model_id, value in model_rates_rmb_per_million.items():
        if not isinstance(model_id, str) or not model_id.strip() or model_id != model_id.strip():
            raise ValueError("Invalid priced model identifier")
        rates[model_id] = _money(value, f"price for {model_id}")
    authorized = _money(authorized_rmb, "authorized_rmb")
    available = _money(available_rmb, "available_rmb")
    non_token = _money(non_token_max_rmb, "non_token_max_rmb")
    highest = max(rates.values())
    worst = Decimal(token_cap) * highest / Decimal(1_000_000) + non_token
    limit = min(authorized, available)
    return CostAssessment(token_cap, authorized, available, highest, non_token,
                          worst, limit, worst <= limit)
