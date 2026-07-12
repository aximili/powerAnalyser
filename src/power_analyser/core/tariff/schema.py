"""Pydantic v2 models for Victorian electricity plan JSON files.

A plan JSON encodes four evaluation layers that the calculation engine
processes for every 30-minute interval:

  1. Fixed overhead   — daily_supply_charge (once per calendar day)
  2. Usage tiers      — flat, ToU, or 3-part "Smart Rate"
  3. Incentive windows — free (or discounted) periods with optional daily cap
  4. Solar FiT tiers  — feed-in credits for B1 export stream

All rates are stored as Decimal to prevent floating-point accumulation
errors across 17,520 intervals per year.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Seven-character day abbreviations used in schedules
DayOfWeek = Literal["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Ordered for consistent presentation
ALL_DAYS: list[DayOfWeek] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAYS: list[DayOfWeek] = ["Mon", "Tue", "Wed", "Thu", "Fri"]
WEEKEND: list[DayOfWeek] = ["Sat", "Sun"]


class TimeRange(BaseModel):
    """A recurring time window on specified days of the week.

    An empty ``days`` list is disallowed — use a tier with an empty
    ``schedule`` list to represent a catch-all (flat-rate) tier.

    Overnight ranges (e.g., 23:00 → 07:00) are supported: when
    ``end <= start`` the range wraps midnight.
    """

    days: list[DayOfWeek] = Field(min_length=1)
    start: time  # inclusive
    end: time    # exclusive; if end <= start the range is overnight


class UsageTier(BaseModel):
    """One rate band within a usage structure.

    An empty ``schedule`` means this tier applies at all times and on all
    days — it acts as the catch-all / default rate (e.g., the off-peak tier
    in a ToU plan, or the single rate in a flat plan).

    A non-empty ``schedule`` restricts the tier to those windows.
    """

    name: str
    rate: Decimal = Field(ge=0, description="Rate in $/kWh")
    schedule: list[TimeRange] = Field(default_factory=list)


class FreeWindow(BaseModel):
    """A zero-cost (promotional) usage window with an optional daily cap.

    If ``fair_use_cap_kwh`` is None the window is uncapped.
    Once the cap is reached, any additional consumption in the window
    is billed at ``overflow_tier``.
    """

    name: str
    schedule: list[TimeRange] = Field(min_length=1)
    fair_use_cap_kwh: float | None = Field(
        default=None,
        ge=0,
        description="Maximum free kWh per day; None = no cap",
    )
    overflow_tier: str = Field(
        description="UsageTier.name to apply after the cap is exhausted"
    )


class FiTTier(BaseModel):
    """A solar feed-in tariff (FiT) credit rate for B1 export.

    An empty ``schedule`` applies the credit at all times (flat FiT).
    A non-empty ``schedule`` enables time-varying or seasonal FiT.
    """

    name: str
    rate: Decimal = Field(ge=0, description="Credit rate in $/kWh")
    schedule: list[TimeRange] = Field(default_factory=list)


class StepTariff(BaseModel):
    """A daily consumption threshold that triggers a higher rate.

    Once cumulative daily consumption exceeds ``threshold_kwh_per_day``,
    the interval that crosses the threshold is split: the portion below
    the threshold is billed at ``tier_below``, the portion above at
    ``tier_above``.
    """

    threshold_kwh_per_day: float = Field(gt=0)
    tier_below: str = Field(description="UsageTier.name for consumption below the threshold")
    tier_above: str = Field(description="UsageTier.name for consumption above the threshold")


class ElectricityPlan(BaseModel):
    """Top-level model representing one retail electricity offer.

    Load this from a JSON file via ``load_plan()`` in ``tariff/loader.py``.
    """

    plan_id: str
    retailer: str
    plan_name: str
    valid_from: str | None = None   # ISO date string, informational only
    valid_to: str | None = None     # ISO date string, informational only
    daily_supply_charge: Decimal = Field(ge=0, description="Fixed cost in $/day")
    usage_tiers: list[UsageTier] = Field(min_length=1)
    free_windows: list[FreeWindow] = Field(default_factory=list)
    fit_tiers: list[FiTTier] = Field(default_factory=list)
    step_tariffs: list[StepTariff] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_tier_references(self) -> "ElectricityPlan":
        """Ensure all tier name references actually exist in usage_tiers."""
        tier_names = {t.name for t in self.usage_tiers}

        for fw in self.free_windows:
            if fw.overflow_tier not in tier_names:
                raise ValueError(
                    f"FreeWindow '{fw.name}' references unknown overflow_tier "
                    f"'{fw.overflow_tier}'. Known tiers: {sorted(tier_names)}"
                )
        for st in self.step_tariffs:
            for attr in ("tier_below", "tier_above"):
                name = getattr(st, attr)
                if name not in tier_names:
                    raise ValueError(
                        f"StepTariff references unknown {attr} '{name}'. "
                        f"Known tiers: {sorted(tier_names)}"
                    )
        return self
