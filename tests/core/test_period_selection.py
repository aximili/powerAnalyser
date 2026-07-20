"""Tests for NEM12 period selection + multi-year averaging.

All offline, pure pandas — builds synthetic multi-day / multi-year
``MeterDataSet`` objects directly (no file parsing, no LLM, no browser).
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from power_analyser.core.comparison.report import ComparisonEngine
from power_analyser.core.ingestion.period import (
    PeriodResolution,
    available_month_days,
    available_years,
    build_clamp_message,
    has_overlap,
    select_period,
    target_calendar_dates,
    years_overlapping_window,
)
from power_analyser.core.ingestion.pipeline import MeterDataSet
from power_analyser.core.simulation.calculator import CostCalculator
from power_analyser.core.tariff.schema import ElectricityPlan

MELBOURNE_TZ = "Australia/Melbourne"


# ── Synthetic dataset builders ────────────────────────────────────────────────


def _day_index(date: datetime.date, n: int = 48) -> pd.DatetimeIndex:
    """Regular 30-min Melbourne-tz index for one date."""
    naive = pd.date_range(
        start=pd.Timestamp(date.year, date.month, date.day), periods=n, freq="30min"
    )
    return naive.tz_localize(MELBOURNE_TZ, ambiguous="infer", nonexistent="shift_forward")


def _make_day_df(date: datetime.date, values: list[float]) -> pd.DataFrame:
    idx = _day_index(date, len(values))
    return pd.DataFrame({"kwh": values}, index=idx)


def _spring_forward_day_index(date: datetime.date) -> pd.DatetimeIndex:
    """Return the 46 real Melbourne timestamps for a spring-forward date.

    Localises a full 48-slot naive range with nonexistent="NaT" then
    strips the two NaT positions (naive 02:00 and 02:30) that fall inside
    the DST gap. The result has 46 distinct tz-aware timestamps with no
    02:00/02:30 and no duplicates at 03:00.
    """
    naive = pd.date_range(
        pd.Timestamp(date.year, date.month, date.day), periods=48, freq="30min"
    )
    localized = naive.tz_localize(MELBOURNE_TZ, ambiguous="infer", nonexistent="NaT")
    return localized[~pd.isnull(localized)]


def _make_meter(
    e1_by_date: dict[datetime.date, list[float]],
    b1_by_date: dict[datetime.date, list[float]] | None = None,
    nmi: str = "TEST",
) -> MeterDataSet:
    """Build a multi-day MeterDataSet from per-date value lists."""
    e1_parts = [_make_day_df(d, v) for d, v in sorted(e1_by_date.items())]
    e1 = pd.concat(e1_parts).sort_index() if e1_parts else pd.DataFrame(columns=["kwh"])

    if b1_by_date:
        b1_parts = [_make_day_df(d, v) for d, v in sorted(b1_by_date.items())]
        b1 = pd.concat(b1_parts).sort_index()
    else:
        b1 = pd.DataFrame(columns=["kwh"])
        b1.index = pd.DatetimeIndex([], tz=MELBOURNE_TZ)

    all_dates = sorted(e1_by_date.keys())
    return MeterDataSet(
        e1=e1,
        b1=b1,
        nmi=nmi,
        start_date=all_dates[0],
        end_date=all_dates[-1],
    )


def _make_two_year_meter(
    dates_2025: list[datetime.date],
    dates_2026: list[datetime.date],
    e1_value: float = 0.5,
    b1_value: float | None = None,
) -> MeterDataSet:
    """Build a meter with the same calendar days across two years."""
    by_date: dict[datetime.date, list[float]] = {}
    b1_by_date: dict[datetime.date, list[float]] | None = None if b1_value is None else {}
    for d in dates_2025 + dates_2026:
        by_date[d] = [e1_value] * 48
        if b1_by_date is not None:
            b1_by_date[d] = [b1_value] * 48
    return _make_meter(by_date, b1_by_date)


# ── 1. available_years + years_overlapping_window ─────────────────────────────


def test_available_years_and_overlapping_window():
    meter = _make_two_year_meter(
        [datetime.date(2025, 6, 1), datetime.date(2025, 6, 2)],
        [datetime.date(2026, 6, 1), datetime.date(2026, 6, 2)],
    )
    assert available_years(meter) == [2025, 2026]

    # Window covers June only
    assert years_overlapping_window(meter, (6, 1), (6, 30)) == [2025, 2026]
    # Window in a month with no data
    assert years_overlapping_window(meter, (1, 1), (1, 31)) == []


def test_years_overlapping_window_wrap():
    # Data on 2025-12-15 and 2026-01-10; wrap window Dec→Feb should catch both
    meter = _make_two_year_meter(
        [datetime.date(2025, 12, 15)],
        [datetime.date(2026, 1, 10)],
    )
    assert years_overlapping_window(meter, (12, 1), (2, 28)) == [2025, 2026]


# ── 2. target_calendar_dates ──────────────────────────────────────────────────


def test_target_calendar_dates_normal():
    dates = target_calendar_dates((6, 1), (6, 3))
    assert dates == [(6, 1), (6, 2), (6, 3)]


def test_target_calendar_dates_full_month_count():
    # 6/1 – 8/31 inclusive
    dates = target_calendar_dates((6, 1), (8, 31))
    # June 30 + July 31 + August 31
    assert len(dates) == 30 + 31 + 31
    assert dates[0] == (6, 1)
    assert dates[-1] == (8, 31)


def test_target_calendar_dates_wrap():
    # 12/1 – 2/28 crosses year-end
    dates = target_calendar_dates((12, 1), (2, 28))
    assert dates[0] == (12, 1)
    assert dates[-1] == (2, 28)
    assert (12, 31) in dates
    assert (1, 1) in dates
    assert (1, 15) in dates
    # Dec 31 + Jan 31 + Feb 28
    assert len(dates) == 31 + 31 + 28


def test_target_calendar_dates_skips_feb_30():
    dates = target_calendar_dates((2, 28), (3, 2))
    assert (2, 29) in dates  # leap reference year includes Feb 29
    assert (2, 30) not in dates
    assert dates == [(2, 28), (2, 29), (3, 1), (3, 2)]


def test_target_calendar_dates_invalid_endpoint_raises():
    with pytest.raises(ValueError):
        target_calendar_dates((2, 30), (3, 5))


def test_target_calendar_dates_wrap_around_ending_feb29():
    """Wrap-around window ending on Feb 29 must not raise ValueError.

    Regression for M3: the wrap-around path used _REF_LEAP_YEAR + 1 (2001) as
    the end year unconditionally. 2001-02-29 does not exist → ValueError. The
    fix uses _REF_LEAP_YEAR (2000) for the end date when to_md == (2, 29).

    Window: Nov 1 → Feb 29 (a ~4-month Australian summer window).
    Expected: the returned list starts at (11, 1) and ends at (2, 29), spanning
    the year-end boundary without raising.

    Hand-verification:
      from_md = (11, 1), to_md = (2, 29) → wrap-around (11 > 2).
      end_year = 2000 (leap), so _ref_date((2, 29), year=2000) = 2000-02-29 ✓.
      The list covers (11,1)…(12,31) + (1,1)…(2,29), inclusive.
    """
    dates = target_calendar_dates((11, 1), (2, 29))
    assert dates[0] == (11, 1)
    assert dates[-1] == (2, 29)
    # Nov: 30 days, Dec: 31, Jan: 31, Feb: 29 (leap) = 121 days
    assert len(dates) == 121
    assert (2, 29) in dates
    assert (12, 31) in dates
    assert (1, 1) in dates


# ── 3. select_period single-year window: identity ─────────────────────────────


def test_select_period_single_year_identity():
    meter = _make_two_year_meter(
        [datetime.date(2025, 6, 1), datetime.date(2025, 6, 2)],
        [datetime.date(2026, 6, 1), datetime.date(2026, 6, 2)],
        e1_value=0.7,
    )
    res = select_period(meter, (6, 1), (6, 30), years=[2025])
    assert res.period_days == 2
    assert res.averaged is False
    assert res.years_used == [2025]
    # Identity: kWh preserved
    total = res.meter.e1["kwh"].sum()
    assert total == pytest.approx(0.7 * 48 * 2)


def test_select_period_chooses_only_that_year():
    meter = _make_two_year_meter(
        [datetime.date(2025, 6, 1)],
        [datetime.date(2026, 6, 1)],
        e1_value=0.5,
    )
    res = select_period(meter, (6, 1), (6, 1), years=[2026])
    assert res.period_days == 1
    assert res.years_used == [2026]
    # The 2026 day is stamped in 2026
    assert all(d.year == 2026 for d in res.meter.e1.index.date)


# ── 4. select_period multi-year Both: mean + flat-rate cross-check ────────────


def test_select_period_multi_year_mean_values():
    # 2025 day = 1.0 kWh/slot, 2026 day = 3.0 kWh/slot → mean = 2.0
    by_date = {
        datetime.date(2025, 6, 1): [1.0] * 48,
        datetime.date(2026, 6, 1): [3.0] * 48,
    }
    meter = _make_meter(by_date)
    res = select_period(meter, (6, 1), (6, 1))  # years=None → both
    assert res.averaged is True
    assert res.period_days == 1
    assert res.years_used == [2025, 2026]
    assert res.meter.e1["kwh"].iloc[0] == pytest.approx(2.0)
    assert res.meter.e1["kwh"].sum() == pytest.approx(2.0 * 48)


def test_select_period_flat_rate_averaged_equals_mean_of_years(flat_rate_plan_dict):
    """Flat-rate plans are weekday-agnostic → averaged total == mean of per-year totals."""
    plan = ElectricityPlan.model_validate(flat_rate_plan_dict)

    # Two years of the same 5 calendar days, different magnitudes
    by_date: dict[datetime.date, list[float]] = {}
    for day in range(1, 6):
        by_date[datetime.date(2025, 6, day)] = [1.0] * 48
        by_date[datetime.date(2026, 6, day)] = [2.0] * 48
    meter = _make_meter(by_date)

    calc = CostCalculator()
    total_2025 = calc.calculate_period(
        _filter_meter_to_year(meter, 2025), plan
    ).total_net
    total_2026 = calc.calculate_period(
        _filter_meter_to_year(meter, 2026), plan
    ).total_net

    res = select_period(meter, (6, 1), (6, 5))
    averaged_total = calc.calculate_period(res.meter, plan).total_net

    expected_mean = (total_2025 + total_2026) / 2
    assert abs(float(averaged_total) - float(expected_mean)) < 0.01

    # Independent ground truth — does NOT route the expected side through the
    # calculator, so this also catches linear calculator bugs the self-referential
    # check above cannot. 5 days × $1.00 supply + 360 kWh (5×48×1.5) × $0.30.
    assert averaged_total == Decimal("5") * Decimal("1.00") + Decimal("360") * Decimal("0.30")
    # Structural fields on the multi-year-both path.
    assert res.averaged is True
    assert res.years_used == [2025, 2026]
    assert res.period_days == 5


def _filter_meter_to_year(meter: MeterDataSet, year: int) -> MeterDataSet:
    e1 = meter.e1[meter.e1.index.year == year]
    b1 = (
        meter.b1[meter.b1.index.year == year]
        if not meter.b1.empty
        else meter.b1
    )
    return MeterDataSet(
        e1=e1,
        b1=b1,
        nmi=meter.nmi,
        start_date=e1.index.date.min(),
        end_date=e1.index.date.max(),
    )


# ── 5. chosen single year only ────────────────────────────────────────────────


def test_select_period_explicit_year_list():
    by_date = {
        datetime.date(2025, 6, 1): [1.0] * 48,
        datetime.date(2026, 6, 1): [9.0] * 48,
    }
    meter = _make_meter(by_date)
    res = select_period(meter, (6, 1), (6, 1), years=[2026])
    assert res.averaged is False
    assert res.years_used == [2026]
    assert res.meter.e1["kwh"].iloc[0] == pytest.approx(9.0)


# ── 6. Wrap-around averaging (summer) ─────────────────────────────────────────


def test_select_period_wrap_around_averaging():
    by_date = {
        datetime.date(2025, 12, 1): [1.0] * 48,
        datetime.date(2026, 2, 1): [3.0] * 48,
    }
    meter = _make_meter(by_date)
    res = select_period(meter, (12, 1), (2, 28))
    # Both calendar days present once each (different years but no duplicate m,d)
    assert res.period_days == 2
    # Dec day stays 1.0, Feb day stays 3.0 (no averaging — different m,d)
    dec_vals = res.meter.e1[res.meter.e1.index.month == 12]["kwh"]
    feb_vals = res.meter.e1[res.meter.e1.index.month == 2]["kwh"]
    assert dec_vals.iloc[0] == pytest.approx(1.0)
    assert feb_vals.iloc[0] == pytest.approx(3.0)


def test_select_period_wrap_around_same_md_across_years():
    by_date = {
        datetime.date(2025, 12, 15): [2.0] * 48,
        datetime.date(2026, 12, 15): [4.0] * 48,
    }
    meter = _make_meter(by_date)
    res = select_period(meter, (12, 1), (1, 31))
    assert res.averaged is True
    assert res.period_days == 1
    assert res.meter.e1["kwh"].iloc[0] == pytest.approx(3.0)


# ── 7. Clamp / overlap ────────────────────────────────────────────────────────


def test_has_overlap_true_and_false():
    window = target_calendar_dates((6, 1), (6, 5))
    assert has_overlap(window, {(6, 3)}) is True
    assert has_overlap(window, {(7, 1)}) is False


def test_build_clamp_message_no_overlap_returns_none():
    window = target_calendar_dates((6, 1), (6, 5))
    assert build_clamp_message(window, {(7, 1)}) is None


def test_build_clamp_message_full_coverage_returns_none():
    window = target_calendar_dates((6, 1), (6, 3))
    avail = {(6, 1), (6, 2), (6, 3), (6, 4)}
    assert build_clamp_message(window, avail) is None


def test_build_clamp_message_partial_start_trim():
    # Window 6/1–6/5, but data starts at 6/3 → trim start to 3/6
    window = target_calendar_dates((6, 1), (6, 5))
    avail = {(6, 3), (6, 4), (6, 5)}
    msg = build_clamp_message(window, avail)
    assert msg is not None
    assert msg == (
        "Part of your selected period has no data (earliest available is 3/6). "
        "Trim the start to 3/6?"
    )


def test_build_clamp_message_partial_end_trim():
    window = target_calendar_dates((6, 1), (6, 5))
    avail = {(6, 1), (6, 2), (6, 3)}
    msg = build_clamp_message(window, avail)
    assert msg is not None
    assert msg == (
        "Part of your selected period has no data (latest available is 3/6). "
        "Trim the end to 3/6?"
    )


def test_build_clamp_message_partial_both():
    window = target_calendar_dates((6, 1), (6, 5))
    avail = {(6, 2), (6, 3), (6, 4)}
    msg = build_clamp_message(window, avail)
    assert msg is not None
    assert msg.startswith("Part of your selected period has no data (available 2/6–4/6).")


# ── 8. Idempotency: single-year file, mode all ────────────────────────────────


def test_select_period_single_year_mode_all_identity():
    by_date = {
        datetime.date(2025, 6, day): [0.5] * 48 for day in range(1, 6)
    }
    meter = _make_meter(by_date)
    res = select_period(meter, (1, 1), (12, 31))  # full year
    assert res.averaged is False
    assert res.period_days == 5
    # Total kWh preserved (DST-free days here)
    assert res.meter.e1["kwh"].sum() == pytest.approx(0.5 * 48 * 5)
    # ComparisonEngine sees the same period_days
    result = ComparisonEngine().compare(
        res.meter,
        [ElectricityPlan.model_validate({
            "plan_id": "f", "retailer": "R", "plan_name": "P",
            "daily_supply_charge": "1.00",
            "usage_tiers": [{"name": "Flat", "rate": "0.30", "schedule": []}],
        })],
    )
    assert result.period_days == 5


# ── 9. DST spring-forward day: correct 46-slot output ────────────────────────


def test_dst_spring_forward_day_preserves_46_slots():
    """A spring-forward day averages to exactly 46 real Melbourne slots.

    Oct 5, 2025 is Melbourne's spring-forward day: clocks jump from 02:00
    AEST to 03:00 AEDT, leaving 46 real half-hour slots.  The new averaging
    logic must output exactly those 46 timestamps — no padding to 48, no
    duplicate 03:00 entries, and no phantom 02:00/02:30 slots.

    Hand-verification:
      48-naive-slot range → tz_localize(nonexistent="NaT") → 2 NaT at
      02:00 and 02:30 → filtered to 46 distinct tz-aware timestamps.
      select_period on a single year must return those 46 timestamps verbatim.
    """
    date = datetime.date(2025, 10, 5)  # spring-forward in Melbourne 2025
    idx = _spring_forward_day_index(date)
    assert len(idx) == 46, "test precondition: spring-forward index must be 46 slots"

    e1 = pd.DataFrame({"kwh": [0.5] * 46}, index=idx)
    b1 = pd.DataFrame(columns=["kwh"])
    b1.index = pd.DatetimeIndex([], tz=MELBOURNE_TZ)
    meter = MeterDataSet(e1=e1, b1=b1, nmi="TEST", start_date=date, end_date=date)

    res = select_period(meter, (10, 5), (10, 5))

    assert res.period_days == 1
    day_rows = res.meter.e1[res.meter.e1.index.date == date]
    assert len(day_rows) == 46, f"Expected 46 slots, got {len(day_rows)}"

    # No 02:00 or 02:30 in the output
    times = {ts.time() for ts in day_rows.index}
    import datetime as _dt
    assert _dt.time(2, 0) not in times, "02:00 must not appear in spring-forward output"
    assert _dt.time(2, 30) not in times, "02:30 must not appear in spring-forward output"

    # No duplicate timestamps
    assert len(day_rows.index) == len(set(day_rows.index)), "Duplicate timestamps found"


def test_spring_forward_day_averaged_cross_year():
    """Spring-forward day (46 slots, 2025) averaged with a normal day (48 slots, 2026).

    Oct 5, 2025 is a spring-forward day; Oct 5, 2026 is a normal AEST day.
    The earliest year (2025) is the reference, so the output must have 46 slots
    (the real Melbourne timestamps for that day).  The kWh value at 15:00 must
    be the exact mean of the two years' 15:00 values, NOT shifted to 14:00.

    Hand-verification:
      2025 values: 1.0 kWh/slot (spring-forward, 46 slots)
      2026 values: 3.0 kWh/slot (normal, 48 slots)
      Canonical index = 2025 timestamps (46 slots, no 02:00/02:30)
      2026 aligned by time-of-day: 02:00 and 02:30 dropped (not in canonical)
      Mean at 15:00 = (1.0 + 3.0) / 2 = 2.0
    """
    sf_date = datetime.date(2025, 10, 5)   # spring-forward, 46 slots
    norm_date = datetime.date(2026, 10, 5)  # normal day, 48 slots

    sf_idx = _spring_forward_day_index(sf_date)
    norm_idx = _day_index(norm_date, 48)

    sf_df = pd.DataFrame({"kwh": [1.0] * 46}, index=sf_idx)
    norm_df = pd.DataFrame({"kwh": [3.0] * 48}, index=norm_idx)

    e1 = pd.concat([sf_df, norm_df]).sort_index()
    all_dates = sorted([sf_date, norm_date])
    meter = MeterDataSet(
        e1=e1,
        b1=pd.DataFrame(columns=["kwh"]),
        nmi="DST_TEST",
        start_date=all_dates[0],
        end_date=all_dates[-1],
    )

    res = select_period(meter, (10, 5), (10, 5))

    assert res.averaged is True
    assert res.years_used == [2025, 2026]

    # Output must have 46 slots (canonical = 2025 spring-forward)
    day_rows = res.meter.e1[res.meter.e1.index.date == sf_date]
    assert len(day_rows) == 46, f"Expected 46 slots, got {len(day_rows)}"

    # No 02:00/02:30 and no duplicates
    times = {ts.time() for ts in day_rows.index}
    import datetime as _dt
    assert _dt.time(2, 0) not in times, "02:00 must not appear in output"
    assert _dt.time(2, 30) not in times, "02:30 must not appear in output"
    assert len(day_rows.index) == len(set(day_rows.index)), "Duplicate timestamps found"

    # kWh at 15:00 is the correct cross-year average (not shifted to 14:00)
    ts_15 = next(ts for ts in day_rows.index if ts.time() == _dt.time(15, 0))
    kwh_15 = day_rows.loc[ts_15, "kwh"]
    assert kwh_15 == pytest.approx(2.0), (
        f"Expected 2.0 kWh at 15:00 (mean of 1.0 and 3.0), got {kwh_15}"
    )


# ── 10. b1 (export) averaged in parallel ──────────────────────────────────────


def test_b1_averaged_in_parallel(flat_rate_plan_dict):
    from copy import deepcopy

    plan_dict = deepcopy(flat_rate_plan_dict)
    plan_dict["fit_tiers"] = [{"name": "FiT", "rate": "0.06", "schedule": []}]
    plan = ElectricityPlan.model_validate(plan_dict)

    by_date = {
        datetime.date(2025, 6, 1): [1.0] * 48,
        datetime.date(2026, 6, 1): [3.0] * 48,
    }
    b1_by_date = {
        datetime.date(2025, 6, 1): [0.2] * 48,
        datetime.date(2026, 6, 1): [0.4] * 48,
    }
    meter = _make_meter(by_date, b1_by_date)

    res = select_period(meter, (6, 1), (6, 1))
    assert res.averaged is True
    assert res.years_used == [2025, 2026]
    assert res.period_days == 1
    # Averaged export = mean(0.2, 0.4) = 0.3 kWh/slot; averaged import = mean(1.0, 3.0) = 2.0.
    assert res.meter.b1["kwh"].iloc[0] == pytest.approx(0.3)
    assert res.meter.e1["kwh"].iloc[0] == pytest.approx(2.0)
    assert not res.meter.b1.empty

    # Solar credit reflects averaged export: 14.4 kWh × $0.06 = $0.864. np.mean
    # dust propagates via Decimal(str(float(...))), so approx is required for the
    # solar figure; total_usage stays exact because E1 averaging is dust-free.
    result = CostCalculator().calculate_period(res.meter, plan)
    assert result.total_solar_credit == pytest.approx(Decimal(str(0.3 * 48 * 0.06)), abs=Decimal("0.01"))
    assert result.total_usage == Decimal("28.80")


# ── Extra: available_month_days + period_days flows through ComparisonResult ──


def test_available_month_days_filtered_by_year():
    meter = _make_two_year_meter(
        [datetime.date(2025, 6, 1)],
        [datetime.date(2026, 7, 1)],
    )
    all_md = available_month_days(meter)
    assert (6, 1) in all_md and (7, 1) in all_md
    only_2025 = available_month_days(meter, years=[2025])
    assert (6, 1) in only_2025 and (7, 1) not in only_2025


def test_three_year_averaging_mean_and_period_days():
    """Three-year average is the arithmetic mean of all three years.

    Every existing multi-year test uses exactly 2 years. This test ensures
    the ``np.nanmean`` path inside ``_average`` scales correctly to 3 years
    (mean of 1.0, 2.0, 3.0 = 2.0, not 1.5 or 2.5) and that ``period_days``
    reflects the number of unique calendar days in the output (1), not the
    total number of input years (3).

    Hand-verification:
      np.nanmean([[1.0]*48, [2.0]*48, [3.0]*48], axis=0) = [2.0]*48.
      Output timestamps come from earliest year (2024), so 1 unique date.
    """
    by_date = {
        datetime.date(2024, 6, 1): [1.0] * 48,  # Year 1
        datetime.date(2025, 6, 1): [2.0] * 48,  # Year 2
        datetime.date(2026, 6, 1): [3.0] * 48,  # Year 3
    }
    meter = _make_meter(by_date)

    res = select_period(meter, (6, 1), (6, 1))  # years=None → average all

    assert res.averaged is True
    assert res.years_used == [2024, 2025, 2026]

    # period_days must be 1 (one unique calendar date in the output),
    # not 3 (which would indicate the input days were stacked rather than averaged).
    assert res.period_days == 1, (
        f"period_days should be 1 (one unique calendar date), got {res.period_days}"
    )

    # Mean of 1.0, 2.0, 3.0 = 2.0 — not 1.5 (2-yr mean of years 1+2) or 2.5
    assert res.meter.e1["kwh"].iloc[0] == pytest.approx(2.0, abs=1e-9), (
        f"Mean of [1.0, 2.0, 3.0] must be 2.0, got {res.meter.e1['kwh'].iloc[0]}"
    )
    assert res.meter.e1["kwh"].sum() == pytest.approx(2.0 * 48, abs=1e-9)


def test_select_period_resolution_fields_populated():
    by_date = {
        datetime.date(2025, 6, 1): [1.0] * 48,
        datetime.date(2026, 6, 1): [3.0] * 48,
    }
    meter = _make_meter(by_date)
    res = select_period(meter, (6, 1), (6, 1))
    assert isinstance(res, PeriodResolution)
    assert res.effective_start_md == (6, 1)
    assert res.effective_end_md == (6, 1)
    assert res.notes  # non-empty averaging note
    assert res.meter.start_date == datetime.date(2025, 6, 1)  # reference = earliest year


# ── Fix 4: interior gap detection in build_clamp_message (M4) ────────────────


def test_build_clamp_message_interior_gap_detected():
    """A gap in the middle of a window (endpoints present) produces a non-None
    message that mentions the gap count.

    Window: Jun 1–5. Data present for Jun 1, 2, 5 only.
    Jun 3 and Jun 4 are interior gaps — neither leading nor trailing missing.

    Hand-verification:
      window = [(6,1),(6,2),(6,3),(6,4),(6,5)]
      avail  = {(6,1),(6,2),(6,5)}
      first = (6,1), last = (6,5)  → start_missing=False, end_missing=False
      interior = window[1:4] = [(6,2),(6,3),(6,4)]
      interior_missing_count = 2  (Jun 3 and Jun 4)
      Expected: non-None message containing "2" and "missing".
    """
    window = target_calendar_dates((6, 1), (6, 5))
    avail = {(6, 1), (6, 2), (6, 5)}  # Jun 3 and Jun 4 absent (interior gaps)
    msg = build_clamp_message(window, avail)
    assert msg is not None, "Interior gap must produce a non-None clamp message"
    assert "2" in msg, f"Message should mention 2 missing days; got: {msg!r}"
    assert "missing" in msg.lower(), f"Message should contain 'missing'; got: {msg!r}"


def test_build_clamp_message_interior_gap_added_to_end_trim_message():
    """When there is both a trailing gap and an interior gap, the message mentions both.

    Window: Jun 1–5. Data for Jun 1, 2, 3 only (Jun 4 and 5 trailing missing).
    Separately, Jun 2 is also absent — making Jun 2 an interior gap.

    Hand-verification:
      avail = {(6,1),(6,3)}
      first=(6,1), last=(6,3), first_idx=0, last_idx=2
      start_missing=False, end_missing=True (Jun 4,5 missing)
      interior_count = 1  (Jun 2 missing, between Jun 1 and Jun 3)
      Expected: message starts with the standard end-trim phrasing and appends
                "Also, 1 day(s) missing within the selected period."
    """
    window = target_calendar_dates((6, 1), (6, 5))
    avail = {(6, 1), (6, 3)}   # Jun 2 (interior) and Jun 4,5 (trailing) absent
    msg = build_clamp_message(window, avail)
    assert msg is not None
    assert "Trim the end" in msg
    assert "1" in msg
    assert "missing" in msg.lower()
