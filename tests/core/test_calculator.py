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


def _day_frame(date: datetime.date, values: list[float]) -> pd.DataFrame:
    """A 30-min Melbourne-tz ``kwh`` DataFrame for one date (arbitrary length)."""
    start = pd.Timestamp(date.year, date.month, date.day).tz_localize(MELBOURNE_TZ)
    idx = pd.date_range(start=start, periods=len(values), freq="30min")
    return pd.DataFrame({"kwh": values}, index=idx)


def _make_multi_day_meter(
    e1_by_date: dict[datetime.date, list[float]],
    b1_by_date: dict[datetime.date, list[float]] | None = None,
) -> MeterDataSet:
    """Build a multi-day MeterDataSet from per-date value lists."""
    e1 = pd.concat([_day_frame(d, v) for d, v in sorted(e1_by_date.items())]).sort_index()
    if b1_by_date:
        b1 = pd.concat([_day_frame(d, v) for d, v in sorted(b1_by_date.items())]).sort_index()
    else:
        b1 = pd.DataFrame(columns=["kwh"])
    dates = sorted(e1_by_date.keys())
    return MeterDataSet(e1=e1, b1=b1, nmi="TEST", start_date=dates[0], end_date=dates[-1])


def _meter_from_index(index: pd.DatetimeIndex, e1_values: list[float]) -> MeterDataSet:
    """Build a single-day MeterDataSet from an explicit (possibly DST-skewed) index."""
    e1 = pd.DataFrame({"kwh": e1_values}, index=index)
    return MeterDataSet(
        e1=e1,
        b1=pd.DataFrame(columns=["kwh"]),
        nmi="TEST",
        start_date=index.date.min(),
        end_date=index.date.max(),
    )


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


# ── Combined "Perfect Day" end-to-end hand math ────────────────────────────────

def test_perfect_day_combined_scenario(perfect_day_plan_dict):
    """All tariff layers at once, validated against hand-computed ground truth.

    Synthetic single day (48 half-hour intervals):
      E1 (import) = 1.0 kWh every interval  → 48 kWh total
      B1 (export) = 0.5 kWh every interval  → 24 kWh total

    Ground-truth napkin math (cents):
      Supply charge                          120
      Super Off-Peak window 11:00-16:00 = 10 intervals (= 10 kWh):
        - first 5 kWh free (cap)               0
        - remaining 5 kWh at Flat $0.30      150
      Standard usage, other 38 intervals
        @ Flat $0.30 (38 kWh * 30)         1140
      Solar export 24 kWh @ $0.05           -120  (credit)
      ---------------------------------------------------------------
      Expected net = 120 + 0 + 150 + 1140 - 120 = 1290 cents ($12.90)
    """
    plan = ElectricityPlan.model_validate(perfect_day_plan_dict)
    # 2024-06-03 is a Monday; the Super Off-Peak window covers all days
    date = datetime.date(2024, 6, 3)
    meter = _make_meter(date, [1.0] * 48, [0.5] * 48)

    result = CostCalculator().calculate_period(meter, plan)

    assert result.total_supply == Decimal("1.20")
    # 5 kWh free (promotional) + 43 kWh billed at flat $0.30 = $12.90 usage
    assert result.total_usage == Decimal("12.90")
    # 5 free kWh valued at the standard $0.30 rate
    assert result.total_promotional_saving == Decimal("1.50")
    # 24 kWh export * $0.05
    assert result.total_solar_credit == Decimal("1.20")
    # 1.20 + 12.90 - 1.20
    assert result.total_net == Decimal("12.90")


# ── Edge cases: DST days ──────────────────────────────────────────────────────

def test_dst_spring_forward_46_intervals(flat_rate_plan_dict):
    """Spring-forward day has only 46 intervals (02:00-03:00 skipped).

    The calculator must bill exactly the intervals present and charge supply
    once — never assume a hardcoded 48.
    """
    plan = ElectricityPlan.model_validate(flat_rate_plan_dict)
    # Real Melbourne AEST→AEDT transition: 02:00/02:30 don't exist → 46 slots
    idx = pd.date_range("2024-10-06 00:00", "2024-10-06 23:30", freq="30min", tz=MELBOURNE_TZ)
    assert len(idx) == 46
    meter = _meter_from_index(idx, [1.0] * 46)

    result = CostCalculator().calculate_period(meter, plan)

    assert result.total_usage == Decimal("46") * Decimal("0.30")
    assert result.total_supply == Decimal("1.00")
    assert result.total_net == Decimal("1.00") + Decimal("46") * Decimal("0.30")


def test_dst_fall_back_50_intervals(flat_rate_plan_dict):
    """Fall-back day has 50 intervals (the 02:00-03:00 hour repeats).

    All 50 must be billed; supply charged once.
    """
    plan = ElectricityPlan.model_validate(flat_rate_plan_dict)
    # Real Melbourne AEDT→AEST transition: 02:00/02:30 occur twice → 50 slots
    idx = pd.date_range("2024-04-07 00:00", "2024-04-07 23:30", freq="30min", tz=MELBOURNE_TZ)
    assert len(idx) == 50
    meter = _meter_from_index(idx, [1.0] * 50)

    result = CostCalculator().calculate_period(meter, plan)

    assert result.total_usage == Decimal("50") * Decimal("0.30")
    assert result.total_supply == Decimal("1.00")
    assert result.total_net == Decimal("1.00") + Decimal("50") * Decimal("0.30")


# ── Edge cases: solar FiT ─────────────────────────────────────────────────────

def test_scheduled_fit_ignores_night_export(flat_rate_plan_dict):
    """A daytime-only FiT schedule must not credit export outside its window.

    Export at 00:00 (night) earns nothing; export at 12:00 (inside the FiT
    window) is credited. This is the 'solar export only happens at night'
    scenario — it must yield zero credit, proving FiT is applied per-interval
    by schedule rather than blanket-crediting any export.
    """
    from copy import deepcopy

    plan_dict = deepcopy(flat_rate_plan_dict)
    plan_dict["fit_tiers"] = [
        {
            "name": "Daytime FiT",
            "rate": "0.10",
            "schedule": [
                {"days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                 "start": "06:00", "end": "20:00"}
            ],
        }
    ]
    plan = ElectricityPlan.model_validate(plan_dict)

    date = datetime.date(2024, 6, 3)
    e1 = [0.0] * 48
    b1 = [0.0] * 48
    b1[0] = 1.0    # 00:00 export → night, outside FiT window → not credited
    b1[24] = 1.0   # 12:00 export → day, inside FiT window → credited
    meter = _make_meter(date, e1, b1)

    result = CostCalculator().calculate_period(meter, plan)

    assert result.total_solar_credit == Decimal("1.0") * Decimal("0.10")
    assert result.total_usage == Decimal("0")
    assert result.total_net == Decimal("1.00") - Decimal("0.10")


def test_export_without_fit_tiers_no_credit(flat_rate_plan_dict):
    """If the plan defines no fit_tiers, export must earn no credit at all."""
    plan = ElectricityPlan.model_validate(flat_rate_plan_dict)  # no fit_tiers
    date = datetime.date(2024, 6, 3)
    meter = _make_meter(date, [0.5] * 48, [0.4] * 48)  # 19.2 kWh export

    result = CostCalculator().calculate_period(meter, plan)

    assert result.total_solar_credit == Decimal("0")
    expected_net = Decimal("1.00") + Decimal("24") * Decimal("0.30")
    assert result.total_net == expected_net


# ── Edge cases: overnight / wraparound windows ────────────────────────────────

def test_overnight_tou_wraparound_window():
    """Peak 22:00-06:00 wraps past midnight (end <= start branch in _time_in_range)."""
    plan = ElectricityPlan.model_validate(
        {
            "plan_id": "test_overnight",
            "retailer": "Test Retailer",
            "plan_name": "Overnight ToU",
            "daily_supply_charge": "1.00",
            "usage_tiers": [
                {
                    "name": "Peak",
                    "rate": "0.40",
                    "schedule": [
                        {"days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                         "start": "22:00", "end": "06:00"}
                    ],
                },
                {"name": "Off-Peak", "rate": "0.20", "schedule": []},
            ],
        }
    )
    date = datetime.date(2024, 6, 3)
    meter = _make_meter(date, [1.0] * 48)

    result = CostCalculator().calculate_period(meter, plan)

    # Peak = 00:00-06:00 (12 intervals) + 22:00-24:00 (4 intervals) = 16
    # Off-peak = remaining 32 intervals
    expected_usage = Decimal("16") * Decimal("0.40") + Decimal("32") * Decimal("0.20")
    assert result.total_usage == expected_usage
    assert result.total_net == Decimal("1.00") + expected_usage


# ── Edge cases: multi-day reset ───────────────────────────────────────────────

def test_free_window_cap_resets_each_day(free_window_plan_dict):
    """The daily fair-use cap must reset per calendar day, not accumulate.

    free_window_plan_dict: window 11:00-14:00 (6 intervals), cap 1.5 kWh,
    overflow → Standard ($0.30). Two identical heavy-window days: if the cap
    reset correctly each day bills 1.5 kWh; if it leaked across days, day 2
    would bill the full 3.0 kWh.
    """
    plan = ElectricityPlan.model_validate(free_window_plan_dict)
    per_day = [0.0] * 48
    for i in range(22, 28):  # 11:00-13:30, 6 intervals × 0.5 kWh = 3.0 kWh
        per_day[i] = 0.5
    meter = _make_multi_day_meter(
        {
            datetime.date(2024, 6, 3): per_day,
            datetime.date(2024, 6, 4): list(per_day),
        }
    )

    result = CostCalculator().calculate_period(meter, plan)

    # Per day: 1.5 kWh free + 1.5 kWh billed at $0.30
    daily_billed = Decimal("1.5") * Decimal("0.30")
    assert result.total_usage == daily_billed * 2
    assert result.total_promotional_saving == daily_billed * 2
    assert result.total_supply == Decimal("1.00") * 2


# ── Edge cases: zero consumption with export ──────────────────────────────────

def test_zero_consumption_day_with_solar_export(flat_rate_plan_dict):
    """A day with no consumption still charges supply and credits export."""
    from copy import deepcopy

    plan_dict = deepcopy(flat_rate_plan_dict)
    plan_dict["fit_tiers"] = [{"name": "FiT", "rate": "0.06", "schedule": []}]
    plan = ElectricityPlan.model_validate(plan_dict)

    date = datetime.date(2024, 6, 3)
    e1 = [0.0] * 48          # no consumption
    b1 = [0.0] * 48
    b1[24] = 10.0            # 10 kWh midday export
    meter = _make_meter(date, e1, b1)

    result = CostCalculator().calculate_period(meter, plan)

    assert result.total_usage == Decimal("0")
    assert result.total_supply == Decimal("1.00")
    assert result.total_solar_credit == Decimal("10") * Decimal("0.06")
    assert result.total_net == Decimal("1.00") - Decimal("10") * Decimal("0.06")
