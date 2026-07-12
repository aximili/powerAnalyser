"""Chronological cost simulation engine.

Processes the E1 and B1 time-series interval by interval, day by day,
applying all four tariff layers in priority order:

  1. Daily supply charge        — flat cost per calendar day
  2. Free / promotional windows — zero-rate with daily fair-use cap
  3. Step tariffs               — rate increases after a daily threshold
  4. Time-of-use tiers          — flat, peak/off-peak, or 3-part Smart Rate
  5. Solar FiT credits          — credited against B1 export

Design note:
  ``decimal.Decimal`` is used for all money arithmetic to prevent
  floating-point accumulation errors across 17,520+ intervals per year.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from ..ingestion.pipeline import MeterDataSet
from ..tariff.schema import ElectricityPlan, FiTTier, FreeWindow, StepTariff, TimeRange, UsageTier


@dataclass
class DailyCost:
    """Cost breakdown for a single calendar day."""

    date: datetime.date
    supply: Decimal          # fixed daily supply charge
    usage: Decimal           # volumetric usage cost (after free windows / step)
    solar_credit: Decimal    # FiT credit earned on B1 export
    promotional_saving: Decimal  # what the free-window usage would have cost at standard rates
    net: Decimal             # supply + usage - solar_credit


@dataclass
class PeriodResult:
    """Aggregated cost result for the full analysis period."""

    daily_costs: list[DailyCost]
    total_supply: Decimal
    total_usage: Decimal
    total_solar_credit: Decimal
    total_promotional_saving: Decimal
    total_net: Decimal
    plan_id: str
    plan_name: str


class CostCalculator:
    """Runs the chronological cost simulation for one plan against one dataset."""

    def calculate_period(
        self,
        meter: MeterDataSet,
        plan: ElectricityPlan,
        e1_override: pd.DataFrame | None = None,
    ) -> PeriodResult:
        """Calculate costs for the entire data period.

        Pass ``e1_override`` (from LoadShiftSimulator) to evaluate a
        load-shifted consumption profile while reusing the original B1 data.
        """
        e1 = e1_override if e1_override is not None else meter.e1
        b1 = meter.b1

        daily_costs: list[DailyCost] = []

        for date in sorted(set(e1.index.date)):
            e1_day = e1[e1.index.date == date]
            b1_day = b1[b1.index.date == date] if not b1.empty else pd.DataFrame()

            daily = self._calculate_day(date, e1_day, b1_day, plan)
            daily_costs.append(daily)

        zero = Decimal("0")
        return PeriodResult(
            daily_costs=daily_costs,
            total_supply=sum((d.supply for d in daily_costs), zero),
            total_usage=sum((d.usage for d in daily_costs), zero),
            total_solar_credit=sum((d.solar_credit for d in daily_costs), zero),
            total_promotional_saving=sum((d.promotional_saving for d in daily_costs), zero),
            total_net=sum((d.net for d in daily_costs), zero),
            plan_id=plan.plan_id,
            plan_name=plan.plan_name,
        )

    # ── Day-level calculation ─────────────────────────────────────────────────

    def _calculate_day(
        self,
        date: datetime.date,
        e1_day: pd.DataFrame,
        b1_day: pd.DataFrame,
        plan: ElectricityPlan,
    ) -> DailyCost:
        """Process all intervals for one calendar day."""
        supply = plan.daily_supply_charge
        usage = Decimal("0")
        solar_credit = Decimal("0")
        promotional_saving = Decimal("0")

        # Running totals used for step-tariff tracking
        daily_consumption_total = Decimal("0")
        daily_promotional_usage = Decimal("0")

        # Pre-resolve step tariff (at most one per plan)
        step: StepTariff | None = plan.step_tariffs[0] if plan.step_tariffs else None
        step_threshold = Decimal(str(step.threshold_kwh_per_day)) if step else Decimal("0")

        b1_values = b1_day["kwh"].tolist() if not b1_day.empty else []

        for i, ts in enumerate(e1_day.index):
            kwh_dec = Decimal(str(float(e1_day.iloc[i]["kwh"])))
            t = ts.time()
            dow = ts.strftime("%a")

            # ── Promotional / free-window check ──────────────────────────────
            fw = _find_active_free_window(plan, dow, t)
            cap = (
                Decimal(str(fw.fair_use_cap_kwh))
                if (fw and fw.fair_use_cap_kwh is not None)
                else None
            )
            in_free_window = fw is not None and (
                cap is None or daily_promotional_usage < cap
            )

            if in_free_window:
                remaining_cap = (cap - daily_promotional_usage) if cap is not None else kwh_dec
                free_kwh = min(kwh_dec, remaining_cap)
                overflow_kwh = kwh_dec - free_kwh

                daily_promotional_usage += free_kwh
                # Track what the free usage would have cost at the standard tier
                base_rate = _find_active_tier(plan, dow, t).rate
                promotional_saving += free_kwh * base_rate

                if overflow_kwh > 0:
                    overflow_tier = _find_tier_by_name(plan, fw.overflow_tier)
                    usage += overflow_kwh * overflow_tier.rate

            else:
                # ── Standard tier with step-tariff logic ─────────────────────
                already_above = step is not None and daily_consumption_total >= step_threshold
                crosses_now = (
                    step is not None
                    and not already_above
                    and daily_consumption_total + kwh_dec > step_threshold
                )

                if crosses_now:
                    # Split this interval at the threshold boundary
                    below_kwh = step_threshold - daily_consumption_total
                    above_kwh = kwh_dec - below_kwh
                    usage += below_kwh * _find_tier_by_name(plan, step.tier_below).rate
                    usage += above_kwh * _find_tier_by_name(plan, step.tier_above).rate
                elif already_above:
                    usage += kwh_dec * _find_tier_by_name(plan, step.tier_above).rate
                else:
                    usage += kwh_dec * _find_active_tier(plan, dow, t).rate

            daily_consumption_total += kwh_dec

            # ── Solar FiT ─────────────────────────────────────────────────────
            if i < len(b1_values) and b1_values[i] > 0:
                b1_kwh = Decimal(str(float(b1_values[i])))
                fit = _find_active_fit_tier(plan, dow, t)
                if fit:
                    solar_credit += b1_kwh * fit.rate

        return DailyCost(
            date=date,
            supply=supply,
            usage=usage,
            solar_credit=solar_credit,
            promotional_saving=promotional_saving,
            net=supply + usage - solar_credit,
        )


# ── Tariff resolution helpers ──────────────────────────────────────────────────


def _find_active_tier(plan: ElectricityPlan, dow: str, t: datetime.time) -> UsageTier:
    """Return the first UsageTier whose schedule covers this day and time.

    Tiers with an empty schedule list act as catch-all (flat-rate or default
    off-peak) and are only used when no time-specific tier matches.
    """
    fallback: UsageTier | None = None
    for tier in plan.usage_tiers:
        if not tier.schedule:
            if fallback is None:
                fallback = tier
            continue
        if _interval_in_schedule(dow, t, tier.schedule):
            return tier
    if fallback is not None:
        return fallback
    return plan.usage_tiers[0]


def _find_tier_by_name(plan: ElectricityPlan, name: str) -> UsageTier:
    """Look up a UsageTier by name (guaranteed valid after schema validation)."""
    for tier in plan.usage_tiers:
        if tier.name == name:
            return tier
    raise ValueError(f"UsageTier '{name}' not found in plan '{plan.plan_id}'")


def _find_active_free_window(
    plan: ElectricityPlan, dow: str, t: datetime.time
) -> FreeWindow | None:
    """Return the first FreeWindow active at this day/time, or None."""
    for fw in plan.free_windows:
        if _interval_in_schedule(dow, t, fw.schedule):
            return fw
    return None


def _find_active_fit_tier(
    plan: ElectricityPlan, dow: str, t: datetime.time
) -> FiTTier | None:
    """Return the active FiT tier for this day/time, or None if no FiT."""
    for fit in plan.fit_tiers:
        if not fit.schedule or _interval_in_schedule(dow, t, fit.schedule):
            return fit
    return None


def _interval_in_schedule(dow: str, t: datetime.time, schedule: list[TimeRange]) -> bool:
    """Return True if (dow, t) falls within any TimeRange in the schedule."""
    for tr in schedule:
        if dow in tr.days and _time_in_range(t, tr):
            return True
    return False


def _time_in_range(t: datetime.time, tr: TimeRange) -> bool:
    """Check whether time t falls within the given TimeRange.

    Handles overnight ranges (e.g., 23:00–07:00) where end <= start.
    """
    if tr.end > tr.start:
        return tr.start <= t < tr.end
    else:
        # Overnight: active from start until midnight, and from midnight until end
        return t >= tr.start or t < tr.end
