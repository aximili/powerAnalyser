"""Tests for the chronological cost calculation engine.

Each test uses a minimal synthetic dataset so expected outputs can be
computed by hand. This avoids brittle dependency on the sample NEM12 file.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pandas as pd
import pytest

from power_analyser.core.ingestion.pipeline import MeterDataSet
from power_analyser.core.simulation.calculator import CostCalculator, DailyCost
from power_analyser.core.tariff.schema import ElectricityPlan


MELBOURNE_TZ = "Australia/Melbourne"


def _make_meter(date: datetime.date, e1_values: list[float], b1_values: list[float] | None = None) -> MeterDataSet:
    """Build a single-day MeterDataSet for testing."""
    start = pd.Timestamp(date.year, date.month, date.day).tz_localize(MELBOURNE_TZ)
    idx = pd.date_range(start=start, periods=48, freq="30min")
    e1 = pd.DataFrame({"kwh": e1_values}, index=idx)
    if b1_values:
        b1 = pd.DataFrame({"kwh": b1_values}, index=idx)
    else:
        b1 = pd.DataFrame(columns=["kwh"])
    return MeterDataSet(e1=e1, b1=b1, nmi="TEST", start_date=date, end_date=date)


# ── Flat rate ──────────────────────────────────────────────────────────────────

def test_flat_rate_cost(flat_rate_plan_dict):
    """Simple flat rate: cost = supply + sum(kWh) * rate."""
    plan = ElectricityPlan.model_validate(flat_rate_plan_dict)
    # 48 intervals of 0.5 kWh = 24 kWh total
    date = datetime.date(2024, 6, 1)
    meter = _make_meter(date, [0.5] * 48)

    result = CostCalculator().calculate_period(meter, plan)

    expected_supply = Decimal("1.00")
    expected_usage = Decimal("24") * Decimal("0.30")
    expected_net = expected_supply + expected_usage

    assert result.total_supply == expected_supply
    assert result.total_usage == expected_usage
    assert result.total_net == expected_net


# ── Time-of-use ────────────────────────────────────────────────────────────────

def test_tou_peak_vs_offpeak(tou_plan_dict):
    """ToU: intervals in peak window (Mon 07:00-23:00) should cost 2× the off-peak rate."""
    plan = ElectricityPlan.model_validate(tou_plan_dict)
    # 2024-06-03 is a Monday
    date = datetime.date(2024, 6, 3)

    # 1 kWh in each interval
    meter = _make_meter(date, [1.0] * 48)

    # Peak: intervals 14-45 = 32 intervals (07:00 = position 14, 23:00 = position 46)
    # Actually 07:00 → index 14 (0-based), 23:00 → index 46 (exclusive)
    # Peak intervals: 14..45 = 32 intervals
    # Off-peak: 0..13 + 46..47 = 16 intervals
    result = CostCalculator().calculate_period(meter, plan)

    peak_kwh = Decimal("32")   # 32 * 1 kWh
    offpeak_kwh = Decimal("16")
    expected_usage = peak_kwh * Decimal("0.40") + offpeak_kwh * Decimal("0.20")
    expected_net = Decimal("1.00") + expected_usage

    assert result.total_usage == expected_usage
    assert result.total_net == expected_net


# ── Free window ────────────────────────────────────────────────────────────────

def test_free_window_within_cap(free_window_plan_dict):
    """Consumption within cap during free window should cost $0 for those intervals."""
    plan = ElectricityPlan.model_validate(free_window_plan_dict)
    # 2024-06-03 Monday, free window 11:00-14:00
    date = datetime.date(2024, 6, 3)

    # 6 free-window intervals (11:00-13:30), each 0.20 kWh → 1.2 kWh total (< 1.5 cap)
    # All other intervals: 0.10 kWh
    values = [0.10] * 48
    for i in range(22, 28):  # intervals 22-27 = 11:00-13:30
        values[i] = 0.20

    meter = _make_meter(date, values)
    result = CostCalculator().calculate_period(meter, plan)

    # Free window kWh = 6 * 0.20 = 1.2  → cost $0
    # Standard kWh = 42 * 0.10 = 4.20 → cost 4.20 * 0.30 = 1.26
    assert result.total_net == Decimal("1.00") + Decimal("4.20") * Decimal("0.30")
    assert result.total_promotional_saving > Decimal("0")


def test_free_window_cap_overflow(free_window_plan_dict):
    """Consumption exceeding cap should bill overflow at standard rate."""
    plan = ElectricityPlan.model_validate(free_window_plan_dict)
    date = datetime.date(2024, 6, 3)

    # 6 free-window intervals, each 0.50 kWh → 3.0 kWh total (> 1.5 cap)
    values = [0.0] * 48
    for i in range(22, 28):
        values[i] = 0.50

    meter = _make_meter(date, values)
    result = CostCalculator().calculate_period(meter, plan)

    # First 1.5 kWh free (3 half-intervals), remaining 1.5 kWh at $0.30
    expected_usage = Decimal("1.5") * Decimal("0.30")
    assert result.total_usage == expected_usage


# ── Step tariff ────────────────────────────────────────────────────────────────

def test_step_tariff_split_at_threshold(step_tariff_plan_dict):
    """The interval crossing the step threshold should be split at the boundary."""
    plan = ElectricityPlan.model_validate(step_tariff_plan_dict)
    date = datetime.date(2024, 6, 3)

    # 48 intervals of 0.5 kWh = 24 kWh; threshold = 5.0 kWh
    # First 10 intervals (5.0 kWh) at $0.20; remaining 38 (19.0 kWh) at $0.40
    # The crossing interval is #10: first 0.0 kWh below (already at threshold exactly),
    # actually: after 10 intervals of 0.5 each = 5.0 kWh exactly → crosses on interval 11

    # Let me use 0.3 kWh each: after 16 intervals = 4.8 kWh, interval 17 brings it to 5.1 kWh
    # → crossing: 0.2 kWh at $0.20, 0.1 kWh at $0.40
    values = [0.3] * 48
    meter = _make_meter(date, values)
    result = CostCalculator().calculate_period(meter, plan)

    # kWh totals: 48 * 0.3 = 14.4 kWh
    # Below 5.0 kWh at $0.20: 5.0 * 0.20 = 1.00
    # Above 5.0 kWh at $0.40: 9.4 * 0.40 = 3.76
    expected_usage = Decimal("5.0") * Decimal("0.20") + Decimal("9.4") * Decimal("0.40")
    assert result.total_usage == expected_usage


# ── Solar FiT ─────────────────────────────────────────────────────────────────

def test_solar_fit_credit(flat_rate_plan_dict):
    """Solar credits should reduce the net cost."""
    from copy import deepcopy

    plan_dict = deepcopy(flat_rate_plan_dict)
    plan_dict["fit_tiers"] = [{"name": "FiT", "rate": "0.06", "schedule": []}]
    plan = ElectricityPlan.model_validate(plan_dict)

    date = datetime.date(2024, 6, 3)
    e1 = [0.5] * 48     # 24 kWh consumption
    b1 = [0.2] * 48     # 9.6 kWh export

    meter = _make_meter(date, e1, b1)
    result = CostCalculator().calculate_period(meter, plan)

    expected_solar = Decimal("9.6") * Decimal("0.06")
    assert result.total_solar_credit == expected_solar
    expected_net = Decimal("1.00") + Decimal("24") * Decimal("0.30") - expected_solar
    assert result.total_net == expected_net
