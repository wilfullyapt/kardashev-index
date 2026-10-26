"""PLACEHOLDER subscription tiers (Wil sets real $ later). Ghost caps reserved for branch C."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tier:
    key: str
    name: str
    price_usd_monthly: int          # PLACEHOLDER
    reads_per_month: int
    ghost_runs_per_month: int       # PLACEHOLDER; unused until branch C
    blurb: str


# PLACEHOLDER dollars and quotas — see ki-paid-api-build-plan.md §2.
TIERS: dict[str, Tier] = {
    "explorer": Tier(
        key="explorer", name="Explorer", price_usd_monthly=49,
        reads_per_month=10_000, ghost_runs_per_month=0,
        blurb="Public Index JSON reads. API key + Customer Portal.",
    ),
    "research": Tier(
        key="research", name="Research", price_usd_monthly=199,
        reads_per_month=100_000, ghost_runs_per_month=2,
        blurb="Higher read quota. Evidence and alerts land in a later release.",
    ),
    "operator": Tier(
        key="operator", name="Operator", price_usd_monthly=499,
        reads_per_month=500_000, ghost_runs_per_month=20,
        blurb="Highest self-serve quota. Ghost runs land in a later release.",
    ),
}

TIER_ORDER = ("explorer", "research", "operator")


def tier_or_none(key: str | None) -> Tier | None:
    return TIERS.get((key or "").strip().lower())
