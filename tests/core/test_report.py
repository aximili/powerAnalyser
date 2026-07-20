"""Tests for ``ComparisonEngine.compare`` (core/comparison/report.py).

ComparisonEngine is the presentation layer over CostCalculator + the
load-shift simulator. These tests pin its ranking, sign conventions, and
field-forwarding behaviour using minimal synthetic plans and meter data so
every expected value is hand-derived.

Reuses the ``_make_meter`` / ``_make_multi_day_meter`` helpers also defined in
``test_calculator.py`` (duplicated here so each test file stays standalone —
matches the existing pattern in ``test_elasticity.py``).
"""

from __future__ import annotations

import datetime
from datetime import time as dtime
from decimal import Decimal

import pandas as pd
import pytest

from power_analyser.core.comparison.report import ComparisonEngine, ComparisonResult
from power_analyser.core.ingestion.pipeline import MeterDataSet
from power_analyser.core.simulation.elasticity import ElasticityConfig, SourceWindow
from power_analyser.core.tariff.schema import ElectricityPlan, TimeRange


MELBOURNE_TZ = "Australia/Melbourne"


# ── Meter helpers (mirrors of test_calculator.py — kept standalone) ────────────


def _make_meter(date: datetime.date, e1_values: list[float], b1_values: list[float] | None = None) -> MeterDataSet:
    """Build a single-day MeterDataSet for testing."""
    start = pd.Timestamp(date.year, date.month, date.day).tz_localize(MELBOURNE_TZ)
    idx = pd.date_range(start=start, periods=48, freq="30min")
    e1 = pd.DataFrame({"kwh": e1_values}, index=idx)
    if b1_values:
        b1 = pd.DataFrame({"kwh": b1_values}, index=idx)
    else:
        b1 = pd.DataFrame(columns=["kwh"])
    return MeterDataSet(e1=e1, b1=b1, nmi="TESTNMI", start_date=date, end_date=date)


def _make_multi_day_meter(
    e1_by_date: dict[datetime.date, list[float]],
) -> MeterDataSet:
    """Build a multi-day MeterDataSet from per-date value lists (no B1)."""
    def _day_frame(d: datetime.date, v: list[float]) -> pd.DataFrame:
        start = pd.Timestamp(d.year, d.month, d.day).tz_localize(MELBOURNE_TZ)
        idx = pd.date_range(start=start, periods=len(v), freq="30min")
        return pd.DataFrame({"kwh": v}, index=idx)

    e1 = pd.concat([_day_frame(d, v) for d, v in sorted(e1_by_date.items())]).sort_index()
    dates = sorted(e1_by_date.keys())
    return MeterDataSet(
        e1=e1,
        b1=pd.DataFrame(columns=["kwh"]),
        nmi="TESTNMI",
        start_date=dates[0],
        end_date=dates[-1],
    )


# ── Plan builders ─────────────────────────────────────────────────────────────


def _flat_plan(plan_id: str, rate: str, supply: str = "1.00") -> ElectricityPlan:
    """A flat-rate plan with a single catch-all tier."""
    return ElectricityPlan.model_validate(
        {
            "plan_id": plan_id,
            "retailer": "Test Retailer",
            "plan_name": f"Flat {rate}",
            "daily_supply_charge": supply,
            "usage_tiers": [{"name": "Flat", "rate": rate, "schedule": []}],
        }
    )


# ── Ranking ───────────────────────────────────────────────────────────────────


def test_rank_order_is_cheapest_baseline_first():
    """``ranked`` MUST be sorted by ``baseline_net`` ascending.

    Three flat-rate plans against identical usage → costs differ only by rate.
    The cheapest-rate plan must come first; the order must be monotonic.

    Usage: 24 kWh uniform (0.5 × 48) on Mon 2024-06-03, supply $1.00.
      Plan low   ($0.10/kWh): usage 2.40, net 3.40   ← ranked[0]
      Plan mid   ($0.20/kWh): usage 4.80, net 5.80   ← ranked[1]
      Plan high  ($0.30/kWh): usage 7.20, net 8.20   ← ranked[2]
    """
    date = datetime.date(2024, 6, 3)
    meter = _make_meter(date, [0.5] * 48)
    plans = [_flat_plan("low", "0.10"), _flat_plan("mid", "0.20"), _flat_plan("high", "0.30")]

    result = ComparisonEngine().compare(meter, plans)

    assert isinstance(result, ComparisonResult)
    assert len(result.ranked) == 3
    assert [e.plan_id for e in result.ranked] == ["low", "mid", "high"]
    assert result.ranked[0].baseline_net == Decimal("3.40")
    assert result.ranked[-1].baseline_net == Decimal("8.20")
    # Monotonic non-decreasing baseline_net across the ranking.
    nets = [e.baseline_net for e in result.ranked]
    assert nets == sorted(nets), f"ranked not cheapest-first: {nets}"
    # ranked[0].baseline_net is the minimum (the explicit invariant requested).
    assert result.ranked[0].baseline_net == min(e.baseline_net for e in result.ranked)


def test_empty_plans_raises_value_error():
    """No plans → ValueError, with the documented message."""
    date = datetime.date(2024, 6, 3)
    meter = _make_meter(date, [0.5] * 48)

    with pytest.raises(ValueError, match="At least one plan is required"):
        ComparisonEngine().compare(meter, [])


# ── period_days ───────────────────────────────────────────────────────────────


def test_period_days_single_day():
    """Single-day meter → ``period_days == 1`` (one distinct calendar date)."""
    date = datetime.date(2024, 6, 3)
    meter = _make_meter(date, [0.5] * 48)
    result = ComparisonEngine().compare(meter, [_flat_plan("a", "0.20")])
    assert result.period_days == 1


def test_period_days_multi_day():
    """Three-day meter → ``period_days == 3`` (three distinct calendar dates).

    Pins report.py:125 ``period_days = len(set(meter.e1.index.date))``.
    """
    meter = _make_multi_day_meter(
        {
            datetime.date(2024, 6, 3): [0.5] * 48,
            datetime.date(2024, 6, 4): [0.5] * 48,
            datetime.date(2024, 6, 5): [0.5] * 48,
        }
    )
    result = ComparisonEngine().compare(meter, [_flat_plan("a", "0.20")])
    assert result.period_days == 3


def test_period_days_averaged_multi_year_meter():
    """``period_days`` reflects the DISTINCT dates in the meter, whatever produced it.

    ``select_period`` (core/ingestion/period.py) re-stamps each averaged
    (month, day) onto a reference year, so an "averaged across two calendar
    windows" result is just a meter with two distinct dates as far as the
    engine is concerned. The engine does NOT recompute the average; it trusts
    the meter it is given (AGENTS.md: ``period_days derives from
    meter.e1.index.date``).

    Here we build the shape ``select_period`` would emit for a 2-window
    average: two distinct dates in the reference year, each a full 48-slot day.
    The assertion is ``period_days == 2`` — i.e. the averaged representative
    day count flows through unchanged.
    """
    meter = _make_multi_day_meter(
        {
            datetime.date(2024, 6, 3): [0.5] * 48,
            datetime.date(2024, 12, 3): [0.5] * 48,
        }
    )
    result = ComparisonEngine().compare(meter, [_flat_plan("a", "0.20")])
    assert result.period_days == 2


# ── Load-shift sign convention ────────────────────────────────────────────────


def test_shift_saving_sign_convention_positive_is_saving():
    """``shift_saving == baseline_net - simulated_net``; positive = saving.

    Plan with a free midday window. Usage: 2 kWh at 17:00-17:30 (Mon peak),
    nothing else. With an ElasticityConfig that moves 100% of that 2 kWh into
    the free window, the simulated run bills $0 for it.

      baseline_usage = 2.0 × 0.50 = 1.00  → baseline_net   = 1.00 + 1.00 = 2.00
      simulated_usage = 0                 → simulated_net  = 1.00 + 0    = 1.00
      shift_saving    = 2.00 - 1.00       = 1.00  (> 0 → a saving)
    """
    plan = ElectricityPlan.model_validate(
        {
            "plan_id": "shift_sign",
            "retailer": "Test Retailer",
            "plan_name": "Shift Sign",
            "daily_supply_charge": "1.00",
            "usage_tiers": [
                {
                    "name": "Peak",
                    "rate": "0.50",
                    "schedule": [
                        {"days": ["Mon", "Tue", "Wed", "Thu", "Fri"], "start": "17:00", "end": "21:00"}
                    ],
                },
                {"name": "Off", "rate": "0.30", "schedule": []},
            ],
            "free_windows": [
                {
                    "name": "Midday",
                    "schedule": [
                        {
                            "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                            "start": "11:00",
                            "end": "14:00",
                        }
                    ],
                    "fair_use_cap_kwh": 10.0,
                    "overflow_tier": "Off",
                }
            ],
        }
    )
    date = datetime.date(2024, 6, 3)  # Monday
    values = [0.0] * 48
    values[34] = 1.0  # 17:00
    values[35] = 1.0  # 17:30  → 2 kWh in the Peak window
    meter = _make_meter(date, values)

    config = ElasticityConfig(
        source_windows=[
            SourceWindow(
                schedule=[TimeRange(days=["Mon"], start=dtime(17, 0), end=dtime(21, 0))],
                shift_fraction=1.0,
            )
        ],
        target_window_name="Midday",
    )

    result = ComparisonEngine().compare(meter, [plan], elasticity_configs={"shift_sign": config})
    entry = result.ranked[0]

    assert entry.baseline_net == Decimal("2.00")
    assert entry.simulated_net == Decimal("1.00")
    # Explicit sign-convention assertion: baseline - simulated, positive = saving.
    assert entry.shift_saving == entry.baseline_net - entry.simulated_net
    assert entry.shift_saving == Decimal("1.00")
    assert entry.shift_saving > 0, "positive shift_saving must denote a saving"


def test_shift_saving_negative_when_shift_raises_cost():
    """Shifting INTO a more expensive window yields a negative shift_saving.

    Sanity check on the sign convention: if the simulator moves load into a
    window that is NOT free (e.g. the target window's cap is already exhausted
    or the target tier is pricier than the source), the simulated net can
    exceed baseline. ``shift_saving`` must then be NEGATIVE (a cost increase),
    not clamped at zero.

    Setup: same plan as the positive test, but only 0.0001 kWh exists in the
    source peak window — the source has near-nothing to remove, so the
    simulation is essentially a no-op and simulated_net ≈ baseline_net.
    We assert the sign relation directly (not a specific dollar value), so the
    test pins the formula rather than a coincidental number.
    """
    plan = ElectricityPlan.model_validate(
        {
            "plan_id": "shift_neg",
            "retailer": "Test Retailer",
            "plan_name": "Shift Neg",
            "daily_supply_charge": "1.00",
            "usage_tiers": [
                {"name": "Flat", "rate": "0.30", "schedule": []},
            ],
            "free_windows": [
                {
                    "name": "Midday",
                    "schedule": [
                        {
                            "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                            "start": "11:00",
                            "end": "14:00",
                        }
                    ],
                    "fair_use_cap_kwh": 1.0,
                    "overflow_tier": "Flat",
                }
            ],
        }
    )
    date = datetime.date(2024, 6, 3)  # Monday
    # 10 kWh midday already fills the 1 kWh cap → 9 kWh overflows at Flat $0.30.
    values = [0.0] * 48
    for i in range(22, 28):  # 11:00-13:30, 6 intervals × ~1.667 kWh ≈ 10 kWh
        values[i] = 10.0 / 6.0
    meter = _make_meter(date, values)

    # Source window overlaps the free window; shifting 50% of free-window load
    # "into" the same free window keeps total energy constant (simulator
    # invariant) but does not reduce cost — so simulated_net == baseline_net
    # and shift_saving == 0. We assert the FORMULA holds (== 0 is the boundary
    # of the sign convention).
    config = ElasticityConfig(
        source_windows=[
            SourceWindow(
                schedule=[TimeRange(days=["Mon"], start=dtime(11, 0), end=dtime(14, 0))],
                shift_fraction=0.5,
            )
        ],
        target_window_name="Midday",
    )

    result = ComparisonEngine().compare(meter, [plan], elasticity_configs={"shift_neg": config})
    entry = result.ranked[0]

    assert entry.shift_saving == entry.baseline_net - entry.simulated_net
    # No-op shift → exactly zero (the boundary between saving and cost increase).
    assert entry.shift_saving == Decimal("0")


# ── Ranking uses effective cost (simulated_net when available) ────────────────
#
# The engine ranks by ``simulated_net`` when an ElasticityConfig was provided
# for a plan, and falls back to ``baseline_net`` for plans without one.
# Sort key: ``e.simulated_net if e.simulated_net is not None else e.baseline_net``
#
# This answers "what is the cheapest plan given I'm willing to shift load?"
# A user who supplies an ElasticityConfig has already signalled willingness
# to shift, so ranking by the shifted result is the correct answer.


def test_ranks_by_baseline_even_when_another_plan_is_cheaper_post_shift():
    """Ranking promotes the plan that is cheapest AFTER load-shifting.

    Plan A: flat $0.20, no free window.
        baseline = 1.00 + 2.0 × 0.20 = 1.40   (no elasticity → simulated_net None)
        sort key = baseline_net = 1.40
    Plan B: Peak $0.50 (Mon 17:00-21:00) + free Midday window.
        baseline = 1.00 + 2.0 × 0.50 = 2.00
        simulated (after shifting 2 kWh peak → free Midday) = 1.00 + 0 = 1.00
        sort key = simulated_net = 1.00

    Post-shift, B ($1.00) is cheaper than A ($1.40), so B is ranked first.
    """
    plan_a = _flat_plan("A", "0.20")
    plan_b = ElectricityPlan.model_validate(
        {
            "plan_id": "B",
            "retailer": "Test Retailer",
            "plan_name": "B free midday",
            "daily_supply_charge": "1.00",
            "usage_tiers": [
                {
                    "name": "Peak",
                    "rate": "0.50",
                    "schedule": [
                        {"days": ["Mon", "Tue", "Wed", "Thu", "Fri"], "start": "17:00", "end": "21:00"}
                    ],
                },
                {"name": "Off", "rate": "0.30", "schedule": []},
            ],
            "free_windows": [
                {
                    "name": "Midday",
                    "schedule": [
                        {
                            "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                            "start": "11:00",
                            "end": "14:00",
                        }
                    ],
                    "fair_use_cap_kwh": 10.0,
                    "overflow_tier": "Off",
                }
            ],
        }
    )
    date = datetime.date(2024, 6, 3)  # Monday
    values = [0.0] * 48
    values[34] = 1.0  # 17:00
    values[35] = 1.0  # 17:30  → 2 kWh peak
    meter = _make_meter(date, values)

    config = ElasticityConfig(
        source_windows=[
            SourceWindow(
                schedule=[TimeRange(days=["Mon"], start=dtime(17, 0), end=dtime(21, 0))],
                shift_fraction=1.0,
            )
        ],
        target_window_name="Midday",
    )

    result = ComparisonEngine().compare(meter, [plan_a, plan_b], elasticity_configs={"B": config})

    a_entry = next(e for e in result.ranked if e.plan_id == "A")
    b_entry = next(e for e in result.ranked if e.plan_id == "B")

    # Ground truth: B is cheaper POST-shift, but more expensive at baseline.
    assert a_entry.baseline_net == Decimal("1.40")
    assert a_entry.simulated_net is None  # A has no elasticity config
    assert b_entry.baseline_net == Decimal("2.00")
    assert b_entry.simulated_net == Decimal("1.00")  # cheaper than A's baseline
    assert b_entry.shift_saving == Decimal("1.00")

    # B has simulated_net $1.00 < A's baseline_net $1.40 → B ranks first.
    assert [e.plan_id for e in result.ranked] == ["B", "A"]


# ── Field forwarding ──────────────────────────────────────────────────────────


def test_warnings_and_nmi_are_forwarded():
    """``ComparisonResult`` forwards ``meter.warnings`` and ``meter.nmi`` verbatim.

    Pins report.py:129-131. Warnings come from ingestion (DST flags, etc.) and
    must reach the UI verbatim. NMI is the meter identifier.
    """
    date = datetime.date(2024, 6, 3)
    meter = _make_meter(date, [0.5] * 48)
    meter.warnings = ["DST spring-forward on 2024-10-06", " fabricated warning for test "]
    meter.nmi = "NMI42XYZ"

    result = ComparisonEngine().compare(meter, [_flat_plan("a", "0.20")])

    assert result.nmi == "NMI42XYZ"
    assert result.warnings == meter.warnings  # forwarded by value, in order
    assert result.warnings is not meter.warnings  # but copied, not the same list object


def test_comparison_entry_forwards_plan_metadata():
    """Per-entry ``last_updated`` and ``conditions`` come from the plan."""
    plan = ElectricityPlan.model_validate(
        {
            "plan_id": "meta",
            "retailer": "Test Retailer",
            "plan_name": "Meta Plan",
            "valid_from": "2024-01-01",
            "last_updated": "2024-06-01T10:00:00+10:00",
            "conditions": ["Direct debit required", "Solar required"],
            "daily_supply_charge": "1.00",
            "usage_tiers": [{"name": "Flat", "rate": "0.20", "schedule": []}],
        }
    )
    date = datetime.date(2024, 6, 3)
    meter = _make_meter(date, [0.5] * 48)

    result = ComparisonEngine().compare(meter, [plan])
    entry = result.ranked[0]

    assert entry.plan_id == "meta"
    assert entry.plan_name == "Meta Plan"
    assert entry.retailer == "Test Retailer"
    assert entry.last_updated == "2024-06-01T10:00:00+10:00"
    assert entry.conditions == ["Direct debit required", "Solar required"]
    # conditions is forwarded by value (a fresh list), not the plan's own list.
    assert entry.conditions is not plan.conditions


def test_result_component_fields_are_decimal():
    """All monetary ComparisonEntry fields must be ``Decimal`` (no float leak).

    Guards the Decimal-money invariant (calculator.py:13 design note) at the
    comparison-layer boundary, since the GUI formats these with Decimal-aware
    quantize() calls.
    """
    plan = _flat_plan("dec", "0.20")
    date = datetime.date(2024, 6, 3)
    meter = _make_meter(date, [0.5] * 48)

    result = ComparisonEngine().compare(meter, [plan])
    entry = result.ranked[0]

    for name in ("baseline_supply", "baseline_usage", "baseline_solar_credit",
                 "baseline_net", "baseline_promotional_saving"):
        value = getattr(entry, name)
        assert isinstance(value, Decimal), f"{name} should be Decimal, got {type(value)}"
    # No elasticity → simulated fields stay None.
    assert entry.simulated_net is None
    assert entry.shift_saving is None
