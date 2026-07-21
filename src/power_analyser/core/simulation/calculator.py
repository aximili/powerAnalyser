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

# Locale-independent weekday names matching the DayOfWeek literals in schema.py.
# pd.Timestamp.weekday() returns 0 for Monday through 6 for Sunday.
_WEEKDAY_NAMES: list[str] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


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

        # Running totals used for step-tariff and free-window cap tracking.
        daily_consumption_total = Decimal("0")
        daily_promotional_usage = Decimal("0")

        # All step tariffs sorted ascending by threshold; passed to _apply_usage_with_step.
        steps: list[StepTariff] = sorted(
            plan.step_tariffs, key=lambda s: s.threshold_kwh_per_day
        )

        # B1 export keyed by timestamp for correct per-interval FiT matching.
        b1_kwh_by_ts: pd.Series = b1_day["kwh"] if not b1_day.empty else pd.Series(dtype=float)

        for ts in e1_day.index:
            kwh_dec = Decimal(str(float(e1_day.at[ts, "kwh"])))
            t = ts.time()
            dow = _day_of_week(ts)

            # ── Promotional / free-window check ──────────────────────────────
            fw = _find_active_free_window(plan, dow, t)
            if fw is not None and _cap_not_exhausted(fw, daily_promotional_usage):
                interval_cost, interval_promo, daily_promotional_usage, overflow_kwh = (
                    _apply_free_window_interval(
                        fw, plan, dow, t, kwh_dec, daily_promotional_usage,
                        daily_consumption_total, steps,
                    )
                )
                usage += interval_cost
                promotional_saving += interval_promo
                # Overflow kWh is regular billable consumption and advances the step accumulator.
                daily_consumption_total += overflow_kwh
            else:
                # ── Standard tier with step-tariff logic ─────────────────────
                # Free-window kWh (daily_promotional_usage) is intentionally
                # excluded from the step accumulator so that free usage cannot
                # consume a consumer's off-peak step allowance.
                interval_cost, daily_consumption_total = _apply_usage_with_step(
                    plan, dow, t, kwh_dec, daily_consumption_total, steps
                )
                usage += interval_cost

            # ── Solar FiT — matched by timestamp, not position ────────────────
            b1_kwh_raw = float(b1_kwh_by_ts.get(ts, 0.0))
            if b1_kwh_raw > 0:
                solar_credit += _apply_fit(plan, dow, t, Decimal(str(b1_kwh_raw)))

        return DailyCost(
            date=date,
            supply=supply,
            usage=usage,
            solar_credit=solar_credit,
            promotional_saving=promotional_saving,
            net=supply + usage - solar_credit,
        )


# ── Per-interval billing sub-functions ────────────────────────────────────────


def _day_of_week(ts: pd.Timestamp) -> str:
    """Return the 3-letter day abbreviation matching DayOfWeek literals.

    Uses pd.Timestamp.weekday() (locale-independent) rather than strftime("%a"),
    which varies with the system LC_TIME setting.
    """
    return _WEEKDAY_NAMES[ts.weekday()]


def _cap_not_exhausted(fw: FreeWindow, daily_promo_used: Decimal) -> bool:
    """Return True if the free-window cap has not yet been fully consumed."""
    if fw.fair_use_cap_kwh is None:
        return True
    return daily_promo_used < Decimal(str(fw.fair_use_cap_kwh))


def _apply_free_window_interval(
    fw: FreeWindow,
    plan: ElectricityPlan,
    dow: str,
    t: datetime.time,
    kwh: Decimal,
    daily_promo_used: Decimal,
    daily_consumption_total: Decimal,
    steps: list[StepTariff],
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Apply one interval's free-window logic.

    Returns ``(usage_cost, promotional_saving, new_daily_promo_used, overflow_kwh)``.

    ``usage_cost`` is non-zero only when this interval straddles the cap
    boundary and has overflow kWh billed at ``fw.overflow_tier``.
    ``overflow_kwh`` is non-zero only when the interval straddles the cap; callers
    must add it to the step-tariff accumulator.
    ``promotional_saving`` is the avoided cost for the free portion, computed with
    step-tariff awareness so heavy users see the correct counterfactual saving.
    """
    cap = Decimal(str(fw.fair_use_cap_kwh)) if fw.fair_use_cap_kwh is not None else None
    remaining = (cap - daily_promo_used) if cap is not None else kwh
    free_kwh = min(kwh, remaining)
    overflow_kwh = kwh - free_kwh

    new_promo_used = daily_promo_used + free_kwh

    # Step-aware promotional saving: what free_kwh would have cost at standard
    # rates, accounting for step-tariff bands already consumed this day.
    # Uses daily_consumption_total before this interval's overflow is counted.
    if steps:
        rem = free_kwh
        pos = daily_consumption_total
        promo_saving = Decimal("0")
        for i, step in enumerate(steps):
            threshold = Decimal(str(step.threshold_kwh_per_day))
            if pos >= threshold:
                continue
            in_band = threshold - pos
            rate = (
                _find_active_tier(plan, dow, t).rate
                if i == 0
                else _find_tier_by_name(plan, steps[i - 1].tier_above).rate
            )
            if rem <= in_band:
                promo_saving += rem * rate
                rem = Decimal("0")
                break
            promo_saving += in_band * rate
            rem -= in_band
            pos = threshold
        if rem > Decimal("0"):
            promo_saving += rem * _find_tier_by_name(plan, steps[-1].tier_above).rate
    else:
        promo_saving = free_kwh * _find_active_tier(plan, dow, t).rate

    overflow_cost = Decimal("0")
    if overflow_kwh > 0 and fw.overflow_tier is not None:
        overflow_tier = _find_tier_by_name(plan, fw.overflow_tier)
        overflow_cost = overflow_kwh * overflow_tier.rate

    return overflow_cost, promo_saving, new_promo_used, overflow_kwh


def _apply_usage_with_step(
    plan: ElectricityPlan,
    dow: str,
    t: datetime.time,
    kwh: Decimal,
    daily_total: Decimal,
    steps: list[StepTariff],
) -> tuple[Decimal, Decimal]:
    """Apply step-tariff and ToU rate logic for one non-free-window interval.

    Returns ``(cost, new_daily_total)``.

    Rate bands (steps sorted ascending by threshold):
      [0,        steps[0].threshold) → _find_active_tier (ToU-aware)
      [steps[0], steps[1].threshold) → steps[0].tier_above
      ...
      [steps[N-1].threshold, ∞)     → steps[-1].tier_above

    Each call may span multiple bands; the interval is split at each threshold
    boundary it crosses.
    """
    if not steps:
        return kwh * _find_active_tier(plan, dow, t).rate, daily_total + kwh

    remaining = kwh
    pos = daily_total
    cost = Decimal("0")

    for i, step in enumerate(steps):
        threshold = Decimal(str(step.threshold_kwh_per_day))
        if pos >= threshold:
            continue  # already above this step

        in_band = threshold - pos
        rate = (
            _find_active_tier(plan, dow, t).rate
            if i == 0
            else _find_tier_by_name(plan, steps[i - 1].tier_above).rate
        )
        if remaining <= in_band:
            cost += remaining * rate
            remaining = Decimal("0")
            break
        cost += in_band * rate
        remaining -= in_band
        pos = threshold

    if remaining > Decimal("0"):
        # Above all step thresholds
        cost += remaining * _find_tier_by_name(plan, steps[-1].tier_above).rate

    return cost, daily_total + kwh


def _apply_fit(
    plan: ElectricityPlan,
    dow: str,
    t: datetime.time,
    b1_kwh: Decimal,
) -> Decimal:
    """Return the FiT credit earned on ``b1_kwh`` of solar export."""
    fit = _find_active_fit_tier(plan, dow, t)
    return b1_kwh * fit.rate if fit else Decimal("0")


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
    raise ValueError(
        f"No UsageTier matches day={dow!r} time={t} on plan '{plan.plan_id}'. "
        f"The plan has a schedule gap — all day/time combinations must be covered "
        f"by at least one tier (use an empty schedule for a catch-all)."
    )


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
