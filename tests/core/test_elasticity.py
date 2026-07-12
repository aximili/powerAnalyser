"""Tests for the load-shifting elasticity simulator.

Key invariant: shifting load preserves total energy — kWh removed from the
source equals kWh added to the target.  The original DataFrame is never mutated.
"""

from __future__ import annotations

import datetime
from datetime import time as dtime

import pandas as pd
import pytest

from power_analyser.core.simulation.elasticity import ElasticityConfig, LoadShiftSimulator, SourceWindow
from power_analyser.core.tariff.schema import ElectricityPlan, TimeRange


MELBOURNE_TZ = "Australia/Melbourne"


def _make_e1(date: datetime.date, values: list[float]) -> pd.DataFrame:
    start = pd.Timestamp(date.year, date.month, date.day).tz_localize(MELBOURNE_TZ)
    idx = pd.date_range(start=start, periods=48, freq="30min")
    return pd.DataFrame({"kwh": values}, index=idx)


@pytest.fixture
def smart_plan(smart_rate_plan_dict):
    return ElectricityPlan.model_validate(smart_rate_plan_dict)


@pytest.fixture
def smart_rate_plan_dict():
    """Minimal plan with a Midday Power Saver free window."""
    return {
        "plan_id": "test_smart",
        "retailer": "R",
        "plan_name": "Smart",
        "daily_supply_charge": "1.00",
        "usage_tiers": [
            {
                "name": "Peak",
                "rate": "0.48",
                "schedule": [{"days": ["Mon", "Tue", "Wed", "Thu", "Fri"], "start": "17:00", "end": "21:00"}],
            },
            {"name": "Off-Peak", "rate": "0.15", "schedule": []},
        ],
        "free_windows": [
            {
                "name": "Midday Power Saver",
                "schedule": [
                    {
                        "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                        "start": "11:00",
                        "end": "14:00",
                    }
                ],
                "fair_use_cap_kwh": 3.0,
                "overflow_tier": "Off-Peak",
            }
        ],
    }


def test_total_energy_is_conserved(smart_plan):
    """Load shifting should not create or destroy energy."""
    date = datetime.date(2024, 6, 3)  # Monday
    values = [0.5] * 48
    e1 = _make_e1(date, values)

    config = ElasticityConfig(
        source_windows=[
            SourceWindow(
                schedule=[TimeRange(days=["Mon", "Tue", "Wed", "Thu", "Fri"], start=dtime(17, 0), end=dtime(21, 0))],
                shift_fraction=0.40,
            )
        ],
        target_window_name="Midday Power Saver",
    )
    simulated = LoadShiftSimulator().simulate(e1, smart_plan, config)

    original_total = e1["kwh"].sum()
    simulated_total = simulated["kwh"].sum()

    assert abs(original_total - simulated_total) < 1e-9, (
        f"Energy not conserved: original={original_total:.6f}, simulated={simulated_total:.6f}"
    )


def test_original_dataframe_not_mutated(smart_plan):
    """simulate() must return a new DataFrame, leaving the original untouched."""
    date = datetime.date(2024, 6, 3)
    values = [0.5] * 48
    e1 = _make_e1(date, values)
    original_values = e1["kwh"].tolist()

    config = ElasticityConfig(
        source_windows=[
            SourceWindow(
                schedule=[TimeRange(days=["Mon", "Tue", "Wed", "Thu", "Fri"], start=dtime(17, 0), end=dtime(21, 0))],
                shift_fraction=0.50,
            )
        ],
        target_window_name="Midday Power Saver",
    )
    LoadShiftSimulator().simulate(e1, smart_plan, config)

    assert e1["kwh"].tolist() == original_values, "Original DataFrame was mutated"


def test_source_intervals_are_reduced(smart_plan):
    """Source window intervals should have lower kWh after simulation."""
    date = datetime.date(2024, 6, 3)  # Monday
    values = [1.0] * 48
    e1 = _make_e1(date, values)

    config = ElasticityConfig(
        source_windows=[
            SourceWindow(
                # Evening peak: 17:00-21:00 = intervals 34-41
                schedule=[TimeRange(days=["Mon", "Tue", "Wed", "Thu", "Fri"], start=dtime(17, 0), end=dtime(21, 0))],
                shift_fraction=0.40,
            )
        ],
        target_window_name="Midday Power Saver",
    )
    simulated = LoadShiftSimulator().simulate(e1, smart_plan, config)

    # Peak intervals (17:00-21:00) = indices 34-41 (8 intervals)
    peak_before = e1.iloc[34:42]["kwh"].mean()
    peak_after = simulated.iloc[34:42]["kwh"].mean()
    assert peak_after < peak_before, "Source intervals should decrease after shifting"


def test_target_intervals_are_increased(smart_plan):
    """Target window intervals should have higher kWh after simulation."""
    date = datetime.date(2024, 6, 3)  # Monday
    values = [0.5] * 48
    e1 = _make_e1(date, values)

    config = ElasticityConfig(
        source_windows=[
            SourceWindow(
                schedule=[TimeRange(days=["Mon", "Tue", "Wed", "Thu", "Fri"], start=dtime(17, 0), end=dtime(21, 0))],
                shift_fraction=0.40,
            )
        ],
        target_window_name="Midday Power Saver",
    )
    simulated = LoadShiftSimulator().simulate(e1, smart_plan, config)

    # Midday window (11:00-14:00) = indices 22-27 (6 intervals)
    midday_before = e1.iloc[22:28]["kwh"].mean()
    midday_after = simulated.iloc[22:28]["kwh"].mean()
    assert midday_after > midday_before, "Target intervals should increase after shifting"


def test_unknown_target_window_returns_unchanged(smart_plan):
    """If the plan has no matching FreeWindow, return the original data unchanged."""
    date = datetime.date(2024, 6, 3)
    values = [0.5] * 48
    e1 = _make_e1(date, values)

    config = ElasticityConfig(
        source_windows=[
            SourceWindow(
                schedule=[TimeRange(days=["Mon"], start=dtime(17, 0), end=dtime(21, 0))],
                shift_fraction=0.40,
            )
        ],
        target_window_name="NONEXISTENT WINDOW",
    )
    result = LoadShiftSimulator().simulate(e1, smart_plan, config)
    assert result is e1, "Should return the original object when no window is found"
