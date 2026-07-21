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


def test_load_shift_weekday_matching_is_locale_independent(smart_plan):
    """Weekday matching must not depend on the system LC_TIME locale.

    Regression: the simulator used ``date.strftime("%a")``, which yields
    localized abbreviations (e.g. 'Mi.' under de_DE) that never match the
    'Mon'..'Sun' schedule literals — silently disabling ALL load-shifting on
    non-English systems. Runs under a non-English locale when one is installed;
    skips otherwise.
    """
    import locale as _locale

    saved = _locale.setlocale(_locale.LC_TIME)
    chosen = None
    for cand in ("de_DE.UTF-8", "fr_FR.UTF-8", "es_ES.UTF-8", "ja_JP.UTF-8"):
        try:
            _locale.setlocale(_locale.LC_TIME, cand)
        except _locale.Error:
            continue
        if datetime.date(2024, 6, 3).strftime("%a") != "Mon":
            chosen = cand
            break
    try:
        if chosen is None:
            pytest.skip("no non-English LC_TIME locale available to exercise the bug")

        date = datetime.date(2024, 6, 3)  # Monday
        values = [0.0] * 48
        for i in range(34, 42):  # 17:00-21:00 Peak = the Mon-Fri source window
            values[i] = 1.0
        e1 = _make_e1(date, values)

        config = ElasticityConfig(
            source_windows=[
                SourceWindow(
                    schedule=[TimeRange(days=["Mon", "Tue", "Wed", "Thu", "Fri"],
                                        start=dtime(17, 0), end=dtime(21, 0))],
                    shift_fraction=0.5,
                )
            ],
            target_window_name="Midday Power Saver",
        )
        simulated = LoadShiftSimulator().simulate(e1, smart_plan, config)

        # Under the locale bug this is a silent no-op; with the fix, load moves
        # out of the 17:00-21:00 source window into the midday target.
        source_after = simulated["kwh"].iloc[34:42].sum()
        assert source_after < sum(values[34:42]), (
            "load-shift did not fire — weekday match is locale-dependent"
        )
        assert abs(e1["kwh"].sum() - simulated["kwh"].sum()) < 1e-9  # energy conserved
    finally:
        _locale.setlocale(_locale.LC_TIME, saved)


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


# ── Exact numerical assertions ────────────────────────────────────────────────


def test_shift_fraction_zero_every_interval_unchanged(smart_plan):
    """shift_fraction=0.0 → no interval changes; total energy is still conserved.

    Pins exact per-interval values, not just the aggregate. A bug that zeroes
    source intervals and refills targets would preserve total but fail here.

    Source window: Mon-Fri 17:00-21:00 (indices 34-41, 8 intervals).
    Non-uniform values ensure the assertion is not trivially satisfied by a
    uniform-fill mistake.
    """
    date = datetime.date(2024, 6, 3)  # Monday
    values = [float(i % 7) * 0.2 + 0.1 for i in range(48)]
    e1 = _make_e1(date, values)

    config = ElasticityConfig(
        source_windows=[
            SourceWindow(
                schedule=[TimeRange(days=["Mon", "Tue", "Wed", "Thu", "Fri"], start=dtime(17, 0), end=dtime(21, 0))],
                shift_fraction=0.0,
            )
        ],
        target_window_name="Midday Power Saver",
    )
    simulated = LoadShiftSimulator().simulate(e1, smart_plan, config)

    assert simulated["kwh"].tolist() == pytest.approx(values, abs=1e-9), (
        "shift_fraction=0.0 must leave every interval unchanged"
    )
    assert simulated["kwh"].sum() == pytest.approx(e1["kwh"].sum(), abs=1e-9)


def test_shift_fraction_one_source_reaches_exactly_zero(smart_plan):
    """shift_fraction=1.0 → all source intervals reach exactly 0 kWh.

    The full removed volume (8 × 1.0 = 8.0 kWh) must be distributed
    across the 6 target (Midday 11:00-14:00) intervals.

    Source:  Mon-Fri 17:00-21:00, indices 34-41 (8 intervals × 1.0 kWh).
    Target:  11:00-14:00,          indices 22-27 (6 intervals × 1.0 kWh).
    """
    date = datetime.date(2024, 6, 3)  # Monday
    values = [1.0] * 48
    e1 = _make_e1(date, values)

    config = ElasticityConfig(
        source_windows=[
            SourceWindow(
                schedule=[TimeRange(days=["Mon", "Tue", "Wed", "Thu", "Fri"], start=dtime(17, 0), end=dtime(21, 0))],
                shift_fraction=1.0,
            )
        ],
        target_window_name="Midday Power Saver",
    )
    simulated = LoadShiftSimulator().simulate(e1, smart_plan, config)

    # Source (8 intervals × 1.0 × 1.0 removed): must be exactly 0.0
    source_vals = simulated.iloc[34:42]["kwh"].tolist()
    assert source_vals == pytest.approx([0.0] * 8, abs=1e-9), (
        f"Source intervals must reach exactly 0.0 kWh after full shift; got {source_vals}"
    )

    # Target (6 intervals): baseline 1.0 + 8.0/6 kWh added to each
    per_target_gain = 8.0 / 6  # = 1.3333...
    target_vals = simulated.iloc[22:28]["kwh"].tolist()
    assert target_vals == pytest.approx([1.0 + per_target_gain] * 6, abs=1e-9), (
        f"Each target interval must receive exactly {per_target_gain:.10f} additional kWh"
    )

    # Energy conservation
    assert simulated["kwh"].sum() == pytest.approx(e1["kwh"].sum(), abs=1e-9)


def test_controlled_fixture_exact_redistribution():
    """Controlled 4-source / 6-target fixture pins exact per-interval kWh.

    Plan: Peak 17:00-19:00 (4 intervals, Mon-Fri); Midday free window
    11:00-14:00 (6 intervals, all days).

    Values: source intervals = 1.0 kWh; target intervals = 0.5 kWh;
    everything else = 0.0 kWh. shift_fraction = 0.5.

    Expected after shift:
      - Each source drops from 1.0 → 0.5 kWh  (4 × 1.0 × 0.5 = 2.0 kWh removed)
      - Each target gains 2.0 / 6 kWh         (2.0 kWh spread over 6 slots)
      - Source mean and target mean verified independently.
    """
    plan = ElectricityPlan.model_validate({
        "plan_id": "test_controlled",
        "retailer": "R",
        "plan_name": "Controlled Fixture",
        "daily_supply_charge": "1.00",
        "usage_tiers": [
            {
                "name": "Peak",
                "rate": "0.48",
                "schedule": [
                    {"days": ["Mon", "Tue", "Wed", "Thu", "Fri"], "start": "17:00", "end": "19:00"}
                ],
            },
            {"name": "Off-Peak", "rate": "0.15", "schedule": []},
        ],
        "free_windows": [
            {
                "name": "Midday",
                "schedule": [
                    {"days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                     "start": "11:00", "end": "14:00"}
                ],
                "fair_use_cap_kwh": None,
                "overflow_tier": "Off-Peak",
            }
        ],
    })

    date = datetime.date(2024, 6, 3)  # Monday
    # Source: 17:00-19:00 (exclusive) = indices 34-37 (4 intervals × 1.0 kWh)
    # Target: 11:00-14:00 (exclusive) = indices 22-27 (6 intervals × 0.5 kWh)
    values = [0.0] * 48
    for i in range(34, 38):   # 4 peak intervals (17:00, 17:30, 18:00, 18:30)
        values[i] = 1.0
    for i in range(22, 28):   # 6 midday intervals (11:00, 11:30, 12:00, 12:30, 13:00, 13:30)
        values[i] = 0.5
    e1 = _make_e1(date, values)

    config = ElasticityConfig(
        source_windows=[
            SourceWindow(
                schedule=[TimeRange(days=["Mon", "Tue", "Wed", "Thu", "Fri"],
                                    start=dtime(17, 0), end=dtime(19, 0))],
                shift_fraction=0.5,
            )
        ],
        target_window_name="Midday",
    )
    simulated = LoadShiftSimulator().simulate(e1, plan, config)

    # 4 × 1.0 × 0.5 = 2.0 kWh removed; each source drops to exactly 0.5
    source_after = simulated.iloc[34:38]["kwh"].tolist()
    assert source_after == pytest.approx([0.5] * 4, abs=1e-9), (
        f"Each source interval should be exactly 0.5 kWh; got {source_after}"
    )
    assert sum(source_after) / len(source_after) == pytest.approx(0.5, abs=1e-9)

    # 2.0 kWh distributed over 6 targets: each gains exactly 2.0/6
    gain_per_target = 2.0 / 6
    expected_target = 0.5 + gain_per_target
    target_after = simulated.iloc[22:28]["kwh"].tolist()
    assert target_after == pytest.approx([expected_target] * 6, abs=1e-9), (
        f"Each target interval should be 0.5 + {gain_per_target:.10f}; got {target_after}"
    )
    assert sum(target_after) / len(target_after) == pytest.approx(expected_target, abs=1e-9)

    # Energy conservation
    assert simulated["kwh"].sum() == pytest.approx(e1["kwh"].sum(), abs=1e-9)


def test_all_zero_source_no_nan_no_change(smart_plan):
    """Source window with all-zero load: result equals original; no NaN introduced.

    When the source intervals are all 0.0 kWh, the total shifted volume is
    0.0, so the target is not touched and the array is returned unchanged.
    The key risks — division by zero inside per-interval arithmetic and NaN
    propagation — must not occur.

    Source: Mon-Fri 17:00-21:00 (indices 34-41) set to 0.0 kWh.
    All other intervals: 0.3 kWh (non-zero so any unintended target change
    is detectable).
    """
    date = datetime.date(2024, 6, 3)  # Monday
    values = [0.3] * 48
    for i in range(34, 42):  # zero out the source window
        values[i] = 0.0
    e1 = _make_e1(date, values)

    config = ElasticityConfig(
        source_windows=[
            SourceWindow(
                schedule=[TimeRange(days=["Mon", "Tue", "Wed", "Thu", "Fri"], start=dtime(17, 0), end=dtime(21, 0))],
                shift_fraction=0.5,
            )
        ],
        target_window_name="Midday Power Saver",
    )
    simulated = LoadShiftSimulator().simulate(e1, smart_plan, config)

    assert not simulated["kwh"].isna().any(), "No NaN must be introduced for all-zero source"
    assert simulated["kwh"].tolist() == pytest.approx(values, abs=1e-9), (
        "Result must equal the original when the source window carries zero load"
    )
