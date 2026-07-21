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

from power_analyser.core.simulation.calculator import _day_of_week


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


def test_free_window_cap_overflow():
    """Free-window cap: the straddle spill AND all later post-cap usage bill at overflow_tier.

    Guards two behaviours:
      (1) the ``overflow_kwh > 0`` straddle block — the interval that crosses
          the cap splits: the free portion is free, the spill bills at
          ``overflow_tier``.
      (2) once the daily cap is exhausted, *later* in-window intervals also bill
          at ``overflow_tier`` — not the normal time-of-use / catch-all rate.

    Behaviour (2) is the consistency fix: post-cap billing used to be split —
    only the straddling interval used ``overflow_tier`` while later in-window
    intervals fell back to the active tier. The engine now bills ALL post-cap
    in-window usage at ``overflow_tier`` (see ``calculator._calculate_day``).

    A DISTINCT ``Overflow`` tier ($0.10) is the overflow target, so the rate is
    genuinely exercised. ``Standard`` ($0.30) is the catch-all / active in-window
    tier; if post-cap usage wrongly fell back to it, total_usage would jump to
    0.97 instead of 0.33.

    Plan (built inline because ``free_window_plan_dict`` hardcodes
    ``overflow_tier="Standard"``):
      - Standard  $0.30  catch-all (the tier active in-window)
      - Overflow  $0.10  overflow target
      - Free window 11:00-14:00, cap 1.5 kWh, overflow_tier="Overflow"

    Mon 2024-06-03; only the 6 free-window intervals (idx 22-27) carry load,
    each 0.8 kWh → 4.8 kWh in-window.

    Hand-math (in_free_window ⟺ daily_promotional_usage < cap):

      i  | time  | kwh | dpr before | in_free | free | overflow | usage add        | saving add
      ---|-------|-----|------------|---------|------|----------|------------------|----------
      22 | 11:00 | 0.8 | 0.0        | True    | 0.8  | 0.0      | —                | 0.8×0.30 = 0.24
      23 | 11:30 | 0.8 | 0.8        | True    | 0.7  | 0.1      | 0.1×0.10 = 0.010 | 0.7×0.30 = 0.21
      24 | 12:00 | 0.8 | 1.5        | cap!*   |  —   |  —       | 0.8×0.10 = 0.080 | —
      25 | 12:30 | 0.8 | 1.5        | cap!*   |  —   |  —       | 0.8×0.10 = 0.080 | —
      26 | 13:00 | 0.8 | 1.5        | cap!*   |  —   |  —       | 0.8×0.10 = 0.080 | —
      27 | 13:30 | 0.8 | 1.5        | cap!*   |  —   |  —       | 0.8×0.10 = 0.080 | —

      (*) still inside the Midday window but the cap is exhausted → billed at
          overflow_tier ($0.10), NOT the Standard catch-all ($0.30).

      total_usage              = 0.010 + 4 × 0.080 = 0.330
      total_promotional_saving = 0.24 + 0.21       = 0.450
      total_net                = 1.00 + 0.33       = 1.330

    Invariant: this test MUST fail if post-cap in-window intervals revert to the
    active/catch-all tier (then total_usage climbs to 0.97).
    """
    plan = ElectricityPlan.model_validate(
        {
            "plan_id": "test_fw_overflow",
            "retailer": "Test Retailer",
            "plan_name": "Free Window Overflow Test",
            "daily_supply_charge": "1.00",
            "usage_tiers": [
                {"name": "Standard", "rate": "0.30", "schedule": []},
                {"name": "Overflow", "rate": "0.10", "schedule": []},
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
                    "fair_use_cap_kwh": 1.5,
                    "overflow_tier": "Overflow",
                }
            ],
        }
    )
    date = datetime.date(2024, 6, 3)  # Monday
    values = [0.0] * 48
    for i in range(22, 28):  # 11:00-13:30, 6 intervals × 0.8 kWh = 4.8 kWh
        values[i] = 0.8

    meter = _make_meter(date, values)
    result = CostCalculator().calculate_period(meter, plan)

    # 0.1 kWh straddle spill + 4 full post-cap intervals (0.8 each), ALL @ Overflow $0.10
    expected_usage = Decimal("0.1") * Decimal("0.10") + Decimal("4") * Decimal("0.8") * Decimal("0.10")
    expected_saving = (Decimal("0.8") + Decimal("0.7")) * Decimal("0.30")
    assert result.total_usage == expected_usage
    assert result.total_usage == Decimal("0.33")
    assert result.total_promotional_saving == expected_saving
    assert result.total_promotional_saving == Decimal("0.45")
    assert result.total_net == Decimal("1.00") + expected_usage


def test_free_window_cap_hit_at_boundary(free_window_plan_dict):
    """Cap consumed by whole intervals (exact multiple) → overflow branch idle.

    Companion to ``test_free_window_cap_overflow``. Here the cap (1.5 kWh) is
    an exact multiple of the per-interval kWh (0.5), so the cap is exhausted by
    whole intervals, ``overflow_kwh`` is always 0, and the overflow_tier branch
    (calculator.py:144-146) never fires. Subsequent in-window intervals fall
    through to the standard branch.

    This is the scenario the old ``test_free_window_cap_overflow`` actually
    exercised. It is preserved here as a boundary case — but it CANNOT detect
    deletion of lines 144-146 (that regression guard lives in
    ``test_free_window_cap_overflow``).

    Plan: free_window_plan_dict (Standard $0.30, Midday 11:00-14:00,
    cap 1.5 kWh, overflow_tier="Standard").

    Mon 2024-06-03; only the 6 free-window intervals (idx 22-27) carry load,
    each 0.5 kWh → 3.0 kWh in-window.

    Hand-math (in_free_window ⟺ daily_promotional_usage < cap):

      i  | time  | kwh | dpr before | in_free | free | overflow | usage add       | saving add
      ---|-------|-----|------------|---------|------|----------|-----------------|----------
      22 | 11:00 | 0.5 | 0.0        | True    | 0.5  | 0.0      | —               | 0.5×0.30 = 0.15
      23 | 11:30 | 0.5 | 0.5        | True    | 0.5  | 0.0      | —               | 0.5×0.30 = 0.15
      24 | 12:00 | 0.5 | 1.0        | True    | 0.5  | 0.0      | —               | 0.5×0.30 = 0.15
      25 | 12:30 | 0.5 | 1.5        | False   |  —   |  —       | 0.5×0.30 = 0.15 | —
      26 | 13:00 | 0.5 | 1.5        | False   |  —   |  —       | 0.5×0.30 = 0.15 | —
      27 | 13:30 | 0.5 | 1.5        | False   |  —   |  —       | 0.5×0.30 = 0.15 | —

      total_usage              = 3 × 0.15 = 0.45  (= 1.5 × 0.30)
      total_promotional_saving = 3 × 0.15 = 0.45  (= 1.5 × 0.30)
      total_net                = 1.00 + 0.45 = 1.45
    """
    plan = ElectricityPlan.model_validate(free_window_plan_dict)
    date = datetime.date(2024, 6, 3)  # Monday
    values = [0.0] * 48
    for i in range(22, 28):  # 11:00-13:30, 6 intervals × 0.5 kWh = 3.0 kWh
        values[i] = 0.5

    meter = _make_meter(date, values)
    result = CostCalculator().calculate_period(meter, plan)

    expected_usage = Decimal("1.5") * Decimal("0.30")
    expected_saving = Decimal("1.5") * Decimal("0.30")
    assert result.total_usage == expected_usage
    assert result.total_usage == Decimal("0.45")
    assert result.total_promotional_saving == expected_saving
    assert result.total_promotional_saving == Decimal("0.45")
    assert result.total_net == Decimal("1.00") + expected_usage


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


def test_step_tariff_threshold_reached_exactly_does_not_split(step_tariff_plan_dict):
    """Cumulative consumption landing exactly on the threshold must NOT split.

    Threshold = 5.0 kWh, uniform 0.5 kWh intervals (48 total = 24 kWh).
    ``crosses_now`` (calculator.py:154) uses strict ``>``, so after the 10th
    interval ``daily_consumption_total == 5.0`` exactly and the predicate is
    False. The next interval falls through to the ``already_above`` branch
    (calculator.py:163, predicate ``>= threshold``), billing the entire
    interval at the High tier. No mid-interval split occurs. This pins the
    ``>`` (not ``>=``) semantics at the boundary.

    Hand-math (rate lookup via _find_active_tier / _find_tier_by_name):

      i  | time   | kwh | dct before | branch                | kWh × rate
      ---|--------|-----|------------|-----------------------|-------------
      0  | 00:00  | 0.5 | 0.0        | else                  | 0.5 × 0.20
      1  | 00:30  | 0.5 | 0.5        | else                  | 0.5 × 0.20
      ...| ...    | ... | ...        | ...                   | ...
      9  | 04:30  | 0.5 | 4.5        | else (5.0 > 5.0 False)| 0.5 × 0.20
      10 | 05:00  | 0.5 | 5.0        | already_above         | 0.5 × 0.40
      11 | 05:30  | 0.5 | 5.5        | already_above         | 0.5 × 0.40
      ...| ...    | ... | ...        | ...                   | ...
      47 | 23:30  | 0.5 | 23.5       | already_above         | 0.5 × 0.40

      Below threshold: 10 × 0.5 × 0.20 = 5.0 × 0.20 = 1.00
      Above threshold: 38 × 0.5 × 0.40 = 19.0 × 0.40 = 7.60
      Expected total usage = 8.60
    """
    plan = ElectricityPlan.model_validate(step_tariff_plan_dict)
    date = datetime.date(2024, 6, 3)
    meter = _make_meter(date, [0.5] * 48)  # 24 kWh total

    result = CostCalculator().calculate_period(meter, plan)

    expected_usage = Decimal("5.0") * Decimal("0.20") + Decimal("19.0") * Decimal("0.40")
    assert result.total_usage == expected_usage
    assert result.total_usage == Decimal("8.60")


def test_step_tariff_exact_threshold_branch_trace(step_tariff_plan_dict, monkeypatch):
    """White-box companion to ``test_step_tariff_threshold_reached_exactly_does_not_split``.

    WHY THIS TEST EXISTS
    --------------------
    The output-only test above cannot distinguish the comparison operators on
    ``calculator.py:150`` (``>=``) and ``calculator.py:154`` (strict ``>``) at
    an exact threshold landing: every operator swap produces a split whose
    ``above_kwh`` (or ``below_kwh``) is zero, so the per-interval cost — and
    therefore every ``PeriodResult`` / ``DailyCost`` Decimal field — is
    identical. The branch that runs differs; the dollars do not.

    This test spies on the tier-resolution helpers to record WHICH branch ran
    per interval, then asserts on that trace. It FAILS if either operator is
    flipped, while passing on the current (correct) engine.

    INVARIANT PINNED
    ----------------
    When ``daily_consumption_total + kwh_dec == step_threshold`` EXACTLY for an
    interval, that interval MUST bill entirely at the below rate — i.e. the
    ``else`` branch fires (a single ``_find_active_tier`` call), NOT
    ``crosses_now``. Equivalently: ``crosses_now`` uses strict ``>`` and
    ``already_above`` uses ``>=``. The interval that brings ``dct`` onto the
    threshold is NOT split; the NEXT interval is billed entirely at
    ``tier_above`` via ``already_above``.

    DATASET
    -------
    step_tariff_plan_dict: threshold = 5.0 kWh, tier_below = "Low" ($0.20),
    tier_above = "High" ($0.40). Mon 2024-06-03, uniform 0.5 kWh × 48 = 24 kWh.
    The threshold is landed-on EXACTLY at the end of interval #9
    (dct 4.5 → 5.0); no interval straddles it.

    SPY
    ---
    ``_find_tier_by_name`` and ``_find_active_tier`` are monkeypatched with
    wrappers that append to a shared ordered ``trace`` (preserving the original
    return values, so numeric output is unchanged). The trace is then parsed
    into a per-interval branch label:

      trace pattern per interval            → branch label
      --------------------------------------|----------------
      ("active", None)                      → "else"           (calculator.py:165-166)
      ("by_name", tier_above)               → "already_above"  (calculator.py:163-164)
      ("by_name", tier_below), ("by_name", tier_above) → "crosses_now" (calculator.py:157-162)

    (No free windows on this plan, so the promotional branches at
    calculator.py:141/145 never fire and add no extra helper calls.)

    HAND-MATH (interval index | dct-before | branch | below_kwh | above_kwh):

      i  | dct before | branch        | below_kwh | above_kwh | rate applied
      ---|------------|---------------|-----------|-----------|------------
      0  | 0.0        | else          | 0.5       | 0.0       | Low 0.20
      ...| ...        | ...           | ...       | ...       | ...
      9  | 4.5        | else          | 0.5       | 0.0       | Low 0.20  ← lands on 5.0 exactly
      10 | 5.0        | already_above | 0.0       | 0.5       | High 0.40
      11 | 5.5        | already_above | 0.0       | 0.5       | High 0.40
      ...| ...        | ...           | ...       | ...       | ...
      47 | 23.5       | already_above | 0.0       | 0.5       | High 0.40

      Expected branch sequence: 10× "else" (i=0..9) + 38× "already_above" (i=10..47).
      Expected "crosses_now" count: 0.

    REGRESSION SIGNALS
    ------------------
      - Flip ``>`` → ``>=`` on calculator.py:154: interval 9 becomes
        ``crosses_now`` (below_kwh=0.5, above_kwh=0) → trace gains a
        ``("by_name", "Low")`` call → branches[9] == "crosses_now". FAIL.
      - Flip ``>=`` → ``>`` on calculator.py:150: interval 10 becomes
        ``crosses_now`` (below_kwh=0, above_kwh=0.5) → trace gains a
        ``("by_name", "Low")`` call → branches[10] == "crosses_now". FAIL.
    """
    from power_analyser.core.simulation import calculator as calc_module

    plan = ElectricityPlan.model_validate(step_tariff_plan_dict)
    step = plan.step_tariffs[0]
    tier_below_name = step.tier_below  # "Low"
    tier_above_name = step.tier_above  # "High"
    threshold = Decimal(str(step.threshold_kwh_per_day))  # 5.0

    date = datetime.date(2024, 6, 3)
    kwh_per_interval = Decimal("0.5")
    meter = _make_meter(date, [float(kwh_per_interval)] * 48)  # 24 kWh total

    # Shared ordered trace of tier-resolution calls.
    trace: list[tuple[str, str | None]] = []
    original_by_name = calc_module._find_tier_by_name
    original_active = calc_module._find_active_tier

    def spy_by_name(plan_arg, name):
        trace.append(("by_name", name))
        return original_by_name(plan_arg, name)

    def spy_active(plan_arg, dow, t):
        trace.append(("active", None))
        return original_active(plan_arg, dow, t)

    monkeypatch.setattr(calc_module, "_find_tier_by_name", spy_by_name)
    monkeypatch.setattr(calc_module, "_find_active_tier", spy_active)

    result = CostCalculator().calculate_period(meter, plan)

    # Parse the ordered trace into a per-interval branch label.
    branches: list[str] = []
    i = 0
    while i < len(trace):
        kind, name = trace[i]
        if kind == "active":
            branches.append("else")
            i += 1
        elif kind == "by_name" and name == tier_below_name:
            # crosses_now always emits (below, above) in that order.
            assert trace[i + 1] == ("by_name", tier_above_name), (
                f"tier_below lookup not followed by tier_above at trace[{i}]: {trace}"
            )
            branches.append("crosses_now")
            i += 2
        elif kind == "by_name" and name == tier_above_name:
            branches.append("already_above")
            i += 1
        else:
            raise AssertionError(f"unexpected trace entry at {i}: {trace[i]}")

    # ── Global shape: one branch label per interval, no extra/missing calls. ──
    assert len(branches) == 48, f"expected 48 interval branches, got {len(branches)}: {branches}"

    # ── No interval is ever split: the crosses_now branch must NOT fire when ──
    # ── an interval lands exactly on the threshold.                            ──
    crosses_indices = [idx for idx, b in enumerate(branches) if b == "crosses_now"]
    assert crosses_indices == [], (
        f"crosses_now fired at interval(s) {crosses_indices} — strict `>` on "
        f"calculator.py:154 and `>=` on calculator.py:150 are both required. "
        f"branches={branches}"
    )

    # ── The exact-threshold-landing interval (i=9, dct 4.5 → 5.0) bills the ───
    # ── WHOLE interval at the below rate via the `else` branch.                ──
    assert branches[9] == "else", (
        f"interval 9 (lands exactly on threshold) must use the else branch, "
        f"got {branches[9]!r}. This pins `>` (not `>=`) on calculator.py:154."
    )

    # ── The NEXT interval (i=10, dct 5.0 → 5.5) bills the WHOLE interval at ───
    # ── the above rate via `already_above`.                                    ──
    assert branches[10] == "already_above", (
        f"interval 10 (first interval after reaching the threshold) must use "
        f"the already_above branch, got {branches[10]!r}. This pins `>=` "
        f"(not `>`) on calculator.py:150."
    )

    # ── Full expected sequence: 10× else (i=0..9) + 38× already_above (i=10..47).
    expected_branches = ["else"] * 10 + ["already_above"] * 38
    assert branches == expected_branches, (
        f"branch sequence mismatch.\n  expected: {expected_branches}\n  actual:   {branches}"
    )

    # ── Call-count cross-checks (informative on failure). ─────────────────────
    active_calls = sum(1 for k, _ in trace if k == "active")
    below_calls = sum(1 for k, n in trace if k == "by_name" and n == tier_below_name)
    above_calls = sum(1 for k, n in trace if k == "by_name" and n == tier_above_name)
    assert active_calls == 10, f"_find_active_tier call count: expected 10, got {active_calls}"
    assert below_calls == 0, (
        f"_find_tier_by_name(tier_below) must never be called under exact-threshold "
        f"landing, got {below_calls} call(s)"
    )
    assert above_calls == 38, (
        f"_find_tier_by_name(tier_above) call count: expected 38, got {above_calls}"
    )

    # ── Sanity: threshold arithmetic is exact (no float drift), and the ───────
    # ── numeric output still matches the output-only test's invariant. ────────
    assert Decimal("10") * kwh_per_interval == threshold, "10 × 0.5 must equal 5.0 exactly"
    assert result.total_usage == Decimal("5.0") * Decimal("0.20") + Decimal("19.0") * Decimal("0.40")
    assert result.total_usage == Decimal("8.60")


def test_step_tariff_with_tou_schedule_above_threshold():
    """ToU + step interaction — CHARACTERISING CURRENT BEHAVIOUR.

    The schema.py StepTariff docstring says "the portion above [billed at]
    tier_above". The calculator's ``already_above`` branch
    (calculator.py:163-164) bills the whole interval at
    ``_find_tier_by_name(plan, step.tier_above).rate`` — i.e. StepHigh,
    ignoring the time-of-use tier that would otherwise apply. The ``else``
    branch (calculator.py:166) does call ``_find_active_tier``, so
    below-threshold intervals DO honour ToU.

    Ambiguity surfaced: above the threshold, is ToU meant to be overridden
    by ``tier_above`` or should ToU still apply? Per the docstring and the
    ``already_above`` branch, the answer the current code gives is YES —
    ``tier_above`` overrides ToU. This test pins THAT behaviour (not a
    desired-but-unimplemented one).

    Plan:
      - Peak     $0.40  Mon-Fri 07:00-23:00
      - Off-Peak $0.20  catch-all
      - StepHigh $0.50  flat
      - step threshold 5.0 kWh, tier_below=Off-Peak, tier_above=StepHigh

    Mon 2024-06-03, uniform 0.5 kWh × 48 = 24 kWh. Threshold (5.0 kWh) is
    reached exactly at interval 9, so interval 10 onwards is ``already_above``.

    Hand-math (CURRENT behaviour — StepHigh overrides ToU above threshold):

      range        | count | kwh  | branch        | rate used     | cost
      -------------|-------|------|---------------|---------------|-----
      00:00-05:00  | 10    | 5.0  | else          | Off-Peak 0.20 | 1.00  (below threshold, ToU respected)
      05:00-07:00  | 4     | 2.0  | already_above | StepHigh 0.50 | 1.00  (ToU would say Off-Peak $0.20)
      07:00-23:00  | 32    | 16.0 | already_above | StepHigh 0.50 | 8.00  (ToU would say Peak $0.40)
      23:00-24:00  | 2     | 1.0  | already_above | StepHigh 0.50 | 0.50  (ToU would say Off-Peak $0.20)

      Total usage (current behaviour) = 1.00 + 1.00 + 8.00 + 0.50 = 10.50
      If ToU were respected above threshold instead, the total would be
      5.0×0.20 + 2.0×0.20 + 16.0×0.40 + 1.0×0.20 = 8.00. The $2.50 gap is
      the behaviour this test pins.
    """
    plan = ElectricityPlan.model_validate(
        {
            "plan_id": "test_step_tou",
            "retailer": "Test Retailer",
            "plan_name": "Step + ToU",
            "daily_supply_charge": "1.00",
            "usage_tiers": [
                {
                    "name": "Peak",
                    "rate": "0.40",
                    "schedule": [
                        {"days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                         "start": "07:00", "end": "23:00"}
                    ],
                },
                {"name": "Off-Peak", "rate": "0.20", "schedule": []},
                {"name": "StepHigh", "rate": "0.50", "schedule": []},
            ],
            "step_tariffs": [
                {"threshold_kwh_per_day": 5.0,
                 "tier_below": "Off-Peak",
                 "tier_above": "StepHigh"}
            ],
        }
    )
    date = datetime.date(2024, 6, 3)  # Monday
    meter = _make_meter(date, [0.5] * 48)  # 24 kWh total

    result = CostCalculator().calculate_period(meter, plan)

    # Current behaviour: above threshold everything is billed at StepHigh $0.50
    # regardless of the ToU schedule.
    expected_usage = Decimal("5.0") * Decimal("0.20") + Decimal("19.0") * Decimal("0.50")
    assert result.total_usage == expected_usage
    assert result.total_usage == Decimal("10.50")


def test_step_tariff_daily_reset_multi_day(step_tariff_plan_dict):
    """``daily_consumption_total`` resets at each midnight, so the step split
    fires independently on day 2.

    The accumulator lives inside ``_calculate_day`` (calculator.py:109) and is
    rebuilt for every calendar day, so the threshold is per-day, not
    period-cumulative. Two days, each crossing the threshold at a different
    interval, verify both the reset and the per-day split.

      - Day 1 (Mon 2024-06-03): uniform 0.5 kWh × 48 = 24 kWh. After interval
        9 dct = 5.0 exactly → interval 10 is ``already_above`` (no split).
      - Day 2 (Tue 2024-06-04): uniform 0.3 kWh × 48 = 14.4 kWh. After
        interval 15 dct = 4.8; interval 16 brings it to 5.1 → split
        (0.2 below + 0.1 above).

    Hand-math day 1 (Low $0.20, High $0.40, threshold 5.0 kWh):

      range        | count | kwh  | branch        | kWh × rate
      -------------|-------|------|---------------|-----------
      i 0..9       | 10    | 5.0  | else          | 5.0 × 0.20 = 1.00
      i 10..47     | 38    | 19.0 | already_above | 19.0 × 0.40 = 7.60
      day 1 usage = 8.60

    Hand-math day 2:

      range        | count | kwh  | branch                | kWh × rate
      -------------|-------|------|-----------------------|----------------------
      i 0..15      | 16    | 4.8  | else                  | 4.8 × 0.20 = 0.96
      i 16 (split) | 1     | 0.3  | crosses_now           | 0.2×0.20 + 0.1×0.40 = 0.08
      i 17..47     | 31    | 9.3  | already_above         | 9.3 × 0.40 = 3.72
      day 2 usage = 4.76

    Total usage = 8.60 + 4.76 = 13.36
    """
    plan = ElectricityPlan.model_validate(step_tariff_plan_dict)
    meter = _make_multi_day_meter(
        {
            datetime.date(2024, 6, 3): [0.5] * 48,  # 24 kWh, exact-boundary cross
            datetime.date(2024, 6, 4): [0.3] * 48,  # 14.4 kWh, mid-interval split
        }
    )

    result = CostCalculator().calculate_period(meter, plan)

    # Per-day usage, via the per-day DailyCost breakdown.
    assert len(result.daily_costs) == 2
    assert result.daily_costs[0].date == datetime.date(2024, 6, 3)
    assert result.daily_costs[1].date == datetime.date(2024, 6, 4)

    day1_expected = Decimal("5.0") * Decimal("0.20") + Decimal("19.0") * Decimal("0.40")
    day2_expected = Decimal("5.0") * Decimal("0.20") + Decimal("9.4") * Decimal("0.40")
    assert result.daily_costs[0].usage == day1_expected
    assert result.daily_costs[0].usage == Decimal("8.60")
    assert result.daily_costs[1].usage == day2_expected
    assert result.daily_costs[1].usage == Decimal("4.76")

    # Aggregated total must equal the sum of per-day usage.
    assert result.total_usage == day1_expected + day2_expected
    assert result.total_usage == Decimal("13.36")


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


# ── Edge cases: plan schedule gap raises instead of silently falling back ─────

def test_find_active_tier_raises_on_schedule_gap():
    """A plan with no catch-all tier and a schedule that doesn't cover all days
    must raise ValueError rather than silently billing at usage_tiers[0].

    Regression for M1: the old code returned plan.usage_tiers[0] when no tier
    matched and no catch-all (empty-schedule) tier existed, silently billing at
    an arbitrary rate.

    Plan: only a Peak tier covering Mon-Fri 07:00-23:00 — Saturday and Sunday
    are not covered, and there is no catch-all tier.

    Hand-verification: date 2024-06-08 is a Saturday. The first interval (00:00)
    is not in any scheduled tier and there is no fallback → ValueError expected.
    """
    plan = ElectricityPlan.model_validate(
        {
            "plan_id": "test_gap",
            "retailer": "Test Retailer",
            "plan_name": "Schedule Gap",
            "daily_supply_charge": "0.00",
            "usage_tiers": [
                {
                    "name": "Peak",
                    "rate": "0.40",
                    "schedule": [
                        {"days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                         "start": "07:00", "end": "23:00"}
                    ],
                },
            ],
        }
    )
    # 2024-06-08 is a Saturday — no tier covers it
    date = datetime.date(2024, 6, 8)
    meter = _make_meter(date, [0.5] * 48)

    with pytest.raises(ValueError, match="schedule gap"):
        CostCalculator().calculate_period(meter, plan)


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


def test_fit_tier_order_independent_catchall_does_not_shadow_scheduled(flat_rate_plan_dict):
    """A flat catch-all FiT listed FIRST must not shadow a time-varying tier.

    Regression for the tier-ordering bug: `_find_active_fit_tier` used to return
    the first empty-schedule tier immediately, so a plan authored as
    [flat 0c catch-all, peak 3c 16:00-23:00] credited ALL export at 0c. This is
    the exact shape of the shipped globird_four4free.json plan. Resolution must
    prefer a matching scheduled tier and use the empty-schedule tier only as a
    fallback (mirroring usage-tier resolution), regardless of list order.
    """
    from copy import deepcopy

    plan_dict = deepcopy(flat_rate_plan_dict)
    plan_dict["fit_tiers"] = [
        {"name": "Flat catch-all", "rate": "0.00", "schedule": []},  # listed FIRST
        {
            "name": "Peak solar",
            "rate": "0.03",
            "schedule": [
                {"days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                 "start": "16:00", "end": "23:00"}
            ],
        },
    ]
    plan = ElectricityPlan.model_validate(plan_dict)

    date = datetime.date(2024, 6, 5)  # a Wednesday
    b1 = [0.0] * 48
    b1[36] = 2.0   # 18:00 export → inside 16:00-23:00 peak window → 3c
    b1[24] = 2.0   # 12:00 export → outside peak → flat catch-all 0c
    meter = _make_meter(date, [0.0] * 48, b1)

    result = CostCalculator().calculate_period(meter, plan)

    # 2 kWh @ 3c (peak) + 2 kWh @ 0c (catch-all) = $0.06
    assert result.total_solar_credit == Decimal("2.0") * Decimal("0.03")


def test_volume_tiered_fit_splits_daily_export_at_threshold(flat_rate_plan_dict):
    """Volume-tiered FiT (fit_steps): premium rate on the first N kWh/day exported, lower after.

    Models "8c on the first 2.5 kWh/day exported, 4c beyond" — a common
    post-deregulation shape. The cumulative daily-export threshold is crossed
    mid-interval, so the crossing interval splits across the two credit rates.
    """
    from copy import deepcopy

    plan_dict = deepcopy(flat_rate_plan_dict)
    plan_dict["fit_tiers"] = [
        {"name": "Premium", "rate": "0.08", "schedule": []},
        {"name": "Excess", "rate": "0.04", "schedule": []},
    ]
    plan_dict["fit_steps"] = [
        {"threshold_kwh_per_day": 2.5, "tier_below": "Premium", "tier_above": "Excess"}
    ]
    plan = ElectricityPlan.model_validate(plan_dict)

    date = datetime.date(2024, 6, 5)
    b1 = [0.0] * 48
    for i in (20, 21, 22, 23):  # 4 x 1.0 kWh = 4.0 kWh exported across the day
        b1[i] = 1.0
    meter = _make_meter(date, [0.0] * 48, b1)

    result = CostCalculator().calculate_period(meter, plan)

    # first 2.5 kWh @ 0.08 (=0.20) + remaining 1.5 kWh @ 0.04 (=0.06) = 0.26
    expected = Decimal("2.5") * Decimal("0.08") + Decimal("1.5") * Decimal("0.04")
    assert result.total_solar_credit == expected
    assert result.total_solar_credit == Decimal("0.26")


def test_volume_tiered_fit_threshold_resets_each_day(flat_rate_plan_dict):
    """The daily-export accumulator for fit_steps must reset each calendar day.

    Two identical days each export 4 kWh; each day independently gets the first
    2.5 kWh at premium — the premium allowance must NOT be consumed across the
    day boundary.
    """
    from copy import deepcopy

    plan_dict = deepcopy(flat_rate_plan_dict)
    plan_dict["fit_tiers"] = [
        {"name": "Premium", "rate": "0.08", "schedule": []},
        {"name": "Excess", "rate": "0.04", "schedule": []},
    ]
    plan_dict["fit_steps"] = [
        {"threshold_kwh_per_day": 2.5, "tier_below": "Premium", "tier_above": "Excess"}
    ]
    plan = ElectricityPlan.model_validate(plan_dict)

    day_export = [0.0] * 48
    for i in (20, 21, 22, 23):
        day_export[i] = 1.0
    d1, d2 = datetime.date(2024, 6, 5), datetime.date(2024, 6, 6)
    meter = _make_multi_day_meter(
        {d1: [0.0] * 48, d2: [0.0] * 48},
        {d1: list(day_export), d2: list(day_export)},
    )

    result = CostCalculator().calculate_period(meter, plan)

    # 0.26 per day x 2 days = 0.52 (not 0.20 + 3.5*0.04 if the accumulator leaked)
    assert result.total_solar_credit == Decimal("2") * Decimal("0.26")


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


# ── Step tariff × free-window interaction ─────────────────────────────────────
#
# AUDIT NOTE (confirmed by execution):
#   The real GloBird FOUR4FREE plan has a 15 kWh off-peak step tariff. The
#   previous engine incremented ``daily_consumption_total`` unconditionally on
#   every interval — including free-window kWh — so free midday usage consumed
#   the off-peak allowance, rerating off-peak usage to the expensive tier.
#
#   Reproducing vector on the real plan (Mon 2024-06-03):
#       values = [0.0]*22 + [1.875]*8 + [0.625]*16 + [0.0]*2
#     → old engine net $4.928 (buggy) vs $4.818 (correct) = $0.11/day over-bill.
#
#   Fix (calculator.py): ``daily_consumption_total`` is now only incremented
#   inside the non-free-window branch, so free-window kWh never count toward
#   the off-peak step allowance. The midday window's 50 kWh cap is handled
#   independently by ``daily_promotional_usage`` (FreeWindow mechanism).


def test_free_window_consumption_should_not_consume_step_threshold():
    """Off-window usage must NOT be rerated to ``tier_above`` just because
    free-window usage pushed the global daily counter past the step threshold.

    This is the CORRECT intended behaviour, now passing after the fix that
    scopes ``daily_consumption_total`` to non-free-window intervals only.

    Plan (synthetic, deliberately minimal):
      - OffPeak   $0.10  catch-all  (the cheap below-threshold rate)
      - Expensive $0.50  catch-all  (the above-threshold rate)
      - Free window "Midday" 11:00-13:00 (4 intervals), uncapped,
        overflow_tier=OffPeak
      - Step tariff: threshold 5.0 kWh, tier_below=OffPeak, tier_above=Expensive

    Mon 2024-06-03. Only two blocks carry load:
      - idx 22-25 (11:00-12:30, free window): 1.5 kWh × 4 = 6.0 kWh (free)
      - idx 30-33 (15:00-16:30, off-window):  0.5 kWh × 4 = 2.0 kWh

    CORRECT hand-math (per-window intent — the bug fix):
      Free-window usage is FREE and must NOT count toward the off-window step
      bucket. The off-window's OWN cumulative is 2.0 kWh, which is < the 5.0 kWh
      threshold, so ALL off-window usage bills at OffPeak $0.10:

        total_usage              = 2.0 × 0.10 = 0.20
        total_promotional_saving = 6.0 × 0.10 = 0.60   (free kWh valued at OffPeak)
        total_net                = 1.00 + 0.20       = 1.20

    """
    plan = ElectricityPlan.model_validate(
        {
            "plan_id": "synthetic_step_free",
            "retailer": "Test Retailer",
            "plan_name": "Step + Free Window Interaction",
            "daily_supply_charge": "1.00",
            "usage_tiers": [
                {"name": "OffPeak", "rate": "0.10", "schedule": []},
                {"name": "Expensive", "rate": "0.50", "schedule": []},
            ],
            "free_windows": [
                {
                    "name": "Midday",
                    "schedule": [
                        {
                            "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                            "start": "11:00",
                            "end": "13:00",
                        }
                    ],
                    "fair_use_cap_kwh": None,
                    "overflow_tier": "OffPeak",
                }
            ],
            "step_tariffs": [
                {"threshold_kwh_per_day": 5.0, "tier_below": "OffPeak", "tier_above": "Expensive"}
            ],
        }
    )
    date = datetime.date(2024, 6, 3)  # Monday
    values = [0.0] * 22 + [1.5] * 4 + [0.0] * 4 + [0.5] * 4 + [0.0] * 14
    assert len(values) == 48
    meter = _make_meter(date, values)

    result = CostCalculator().calculate_period(meter, plan)

    assert result.total_usage == Decimal("2.0") * Decimal("0.10")  # 0.20
    assert result.total_promotional_saving == Decimal("6.0") * Decimal("0.10")  # 0.60
    assert result.total_net == Decimal("1.00") + Decimal("2.0") * Decimal("0.10")  # 1.20


def test_free_window_overflow_advances_step_accumulator():
    """H5 regression: overflow kWh from a capped free window must count toward the
    step-tariff accumulator, so subsequent non-free intervals can cross the threshold.

    Plan (synthetic):
      - Cheap     $0.10  catch-all  (below-threshold rate)
      - Expensive $0.50  catch-all  (above-threshold rate)
      - Overflow  $0.20  catch-all  (overflow-tier for the free window)
      - Free window "Midday" 11:00-13:00 (all days), cap 2.0 kWh/day,
        overflow_tier="Overflow"
      - Step tariff: threshold 3.0 kWh/day, tier_below="Cheap", tier_above="Expensive"

    Mon 2024-06-03. Only two intervals carry load:
      - idx 22 (11:00): 3.5 kWh — in free window, straddles cap
      - idx 23 (11:30): 2.0 kWh — cap exhausted → standard branch

    Hand-math (with Fix 1: overflow_kwh advances the step accumulator):

      idx 22 (11:00) — free window, cap not exhausted (0.0 < 2.0):
        free_kwh = min(3.5, 2.0) = 2.0;  overflow_kwh = 1.5
        overflow_cost = 1.5 × $0.20 = $0.30
        promo_saving: dct=0.0, dct+free=2.0 < threshold=3.0 → all Cheap
          = 2.0 × $0.10 = $0.20
        Fix 1: daily_consumption_total += 1.5 → dct = 1.5
        daily_promotional_usage = 2.0

      idx 23 (11:30) — cap exhausted (2.0 ≥ 2.0) → standard branch:
        pos=1.5, threshold=3.0, in_band=1.5; remaining=2.0 > in_band → SPLIT
          below: 1.5 kWh × $0.10 = $0.15
          above: 0.5 kWh × $0.50 = $0.25
        cost = $0.40;  dct = 3.5

      All other intervals: 0.0 kWh → no cost.

      total_usage              = $0.30 + $0.40 = $0.70
      total_promotional_saving = $0.20
      total_net                = $1.00 + $0.70 = $1.70

    Without Fix 1 (bug): dct=0.0 when idx 23 runs → in_band=3.0 → no split
      → cost = 2.0 × $0.10 = $0.20 → total_usage = $0.50 (under-bills by $0.20).
    The assertion on total_usage pins that the step split fires at idx 23,
    proving overflow_kwh was counted toward the 3.0 kWh threshold.
    """
    plan = ElectricityPlan.model_validate(
        {
            "plan_id": "h5_regression",
            "retailer": "Test Retailer",
            "plan_name": "H5: Free Window Overflow + Step",
            "daily_supply_charge": "1.00",
            "usage_tiers": [
                {"name": "Cheap",     "rate": "0.10", "schedule": []},
                {"name": "Expensive", "rate": "0.50", "schedule": []},
                {"name": "Overflow",  "rate": "0.20", "schedule": []},
            ],
            "free_windows": [
                {
                    "name": "Midday",
                    "schedule": [
                        {
                            "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                            "start": "11:00",
                            "end": "13:00",
                        }
                    ],
                    "fair_use_cap_kwh": 2.0,
                    "overflow_tier": "Overflow",
                }
            ],
            "step_tariffs": [
                {"threshold_kwh_per_day": 3.0, "tier_below": "Cheap", "tier_above": "Expensive"}
            ],
        }
    )
    date = datetime.date(2024, 6, 3)  # Monday
    values = [0.0] * 48
    values[22] = 3.5   # 11:00 — straddles cap, overflow_kwh=1.5
    values[23] = 2.0   # 11:30 — cap exhausted, must cross threshold when counted

    meter = _make_meter(date, values)
    result = CostCalculator().calculate_period(meter, plan)

    # idx 22: overflow cost
    expected_overflow_cost = Decimal("1.5") * Decimal("0.20")  # $0.30

    # idx 23: step split fires because overflow_kwh (1.5) was added to dct
    #   below portion: (3.0 - 1.5) = 1.5 kWh @ Cheap $0.10 = $0.15
    #   above portion: (2.0 - 1.5) = 0.5 kWh @ Expensive $0.50 = $0.25
    expected_split_cost = Decimal("1.5") * Decimal("0.10") + Decimal("0.5") * Decimal("0.50")

    expected_usage = expected_overflow_cost + expected_split_cost
    assert result.total_usage == expected_usage
    assert result.total_usage == Decimal("0.70"), (
        "Step split must fire at idx 23 because overflow_kwh (1.5) advanced dct to 1.5. "
        "total_usage == $0.50 means the split did NOT fire (H5 bug still present)."
    )
    assert result.total_promotional_saving == Decimal("2.0") * Decimal("0.10")  # $0.20
    assert result.total_net == Decimal("1.00") + expected_usage


def test_free_window_overflow_promo_saving_is_step_aware():
    """L1 regression: promotional_saving must use the step-aware rate for the
    above-threshold portion of free_kwh, not just the flat ToU rate.

    Same plan as test_free_window_overflow_advances_step_accumulator.
    Here the free window fires while daily_consumption_total is already
    partially through the step band, so free_kwh spans the threshold.

    Mon 2024-06-03. Two intervals carry load:
      - idx 0  (00:00): 2.0 kWh — pre-free-window, standard branch
      - idx 22 (11:00): 3.5 kWh — free window, straddles cap

    Hand-math:

      idx 0 (00:00) — standard branch:
        pos=0.0, threshold=3.0, in_band=3.0; remaining=2.0 ≤ in_band → all Cheap
        cost = 2.0 × $0.10 = $0.20;  dct = 2.0

      idx 22 (11:00) — free window, cap not exhausted (0.0 < 2.0):
        free_kwh = 2.0;  overflow_kwh = 1.5
        overflow_cost = 1.5 × $0.20 = $0.30

        Step-aware promo_saving (dct=2.0, threshold=3.0, free_kwh=2.0):
          dct+free = 4.0 > threshold=3.0 → split at 3.0
          below_kwh = 3.0 - 2.0 = 1.0 kWh  @ Cheap     $0.10 = $0.10
          above_kwh = 2.0 - 1.0 = 1.0 kWh  @ Expensive $0.50 = $0.50
          promo_saving = $0.60

        Fix 1: dct += 1.5 → dct = 3.5

      All other intervals: 0.0 kWh.

      total_usage              = $0.20 + $0.30 = $0.50
      total_promotional_saving = $0.60
      total_net                = $1.00 + $0.50 = $1.50

    Without L1 fix (bug): promo_saving = 2.0 × $0.10 = $0.20 (flat ToU, understated).
    The assertion pins $0.60 — the correct step-aware value.
    """
    plan = ElectricityPlan.model_validate(
        {
            "plan_id": "l1_regression",
            "retailer": "Test Retailer",
            "plan_name": "L1: Step-Aware Promo Saving",
            "daily_supply_charge": "1.00",
            "usage_tiers": [
                {"name": "Cheap",     "rate": "0.10", "schedule": []},
                {"name": "Expensive", "rate": "0.50", "schedule": []},
                {"name": "Overflow",  "rate": "0.20", "schedule": []},
            ],
            "free_windows": [
                {
                    "name": "Midday",
                    "schedule": [
                        {
                            "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                            "start": "11:00",
                            "end": "13:00",
                        }
                    ],
                    "fair_use_cap_kwh": 2.0,
                    "overflow_tier": "Overflow",
                }
            ],
            "step_tariffs": [
                {"threshold_kwh_per_day": 3.0, "tier_below": "Cheap", "tier_above": "Expensive"}
            ],
        }
    )
    date = datetime.date(2024, 6, 3)  # Monday
    values = [0.0] * 48
    values[0]  = 2.0   # 00:00 — pre-free-window, pushes dct to 2.0
    values[22] = 3.5   # 11:00 — free_kwh=2.0 spans threshold at 3.0

    meter = _make_meter(date, values)
    result = CostCalculator().calculate_period(meter, plan)

    # promo_saving splits free_kwh=2.0 at threshold=3.0, from pos=2.0:
    #   below: (3.0 - 2.0) = 1.0 kWh @ Cheap $0.10 = $0.10
    #   above: (2.0 - 1.0) = 1.0 kWh @ Expensive $0.50 = $0.50
    expected_promo_saving = Decimal("1.0") * Decimal("0.10") + Decimal("1.0") * Decimal("0.50")
    assert result.total_promotional_saving == expected_promo_saving
    assert result.total_promotional_saving == Decimal("0.60"), (
        "Promotional saving must use the step-aware rate. "
        "$0.20 means the flat ToU rate was used for all free_kwh (L1 bug still present)."
    )

    expected_usage = Decimal("2.0") * Decimal("0.10") + Decimal("1.5") * Decimal("0.20")
    assert result.total_usage == expected_usage
    assert result.total_usage == Decimal("0.50")
    assert result.total_net == Decimal("1.00") + expected_usage


def test_second_step_tariff_is_applied():
    """Both step_tariffs are evaluated; the engine iterates all steps in threshold order.

    Previously only step_tariffs[0] was consulted (bug C2). This regression test
    verifies the fix: both thresholds split daily consumption into three bands.

    Plan (synthetic):
      - Low  $0.20  catch-all
      - Mid  $0.30  catch-all
      - High $0.40  catch-all
      - step[0]: threshold  5.0, tier_below=Low, tier_above=Mid
      - step[1]: threshold 10.0, tier_below=Mid, tier_above=High

    Mon 2024-06-03, uniform 0.5 kWh × 48 = 24 kWh.

    Hand-math (correct behaviour — both steps applied):
      Band 0 [0, 5):   10 intervals × 0.5 kWh @ Low  $0.20 = 5.0 × 0.20 = 1.00
      Band 1 [5, 10):  10 intervals × 0.5 kWh @ Mid  $0.30 = 5.0 × 0.30 = 1.50
      Band 2 [10, ∞):  28 intervals × 0.5 kWh @ High $0.40 = 14.0 × 0.40 = 5.60
      total_usage = 8.10
    """
    plan = ElectricityPlan.model_validate(
        {
            "plan_id": "test_two_step",
            "retailer": "Test Retailer",
            "plan_name": "Two Step Tariffs",
            "daily_supply_charge": "1.00",
            "usage_tiers": [
                {"name": "Low", "rate": "0.20", "schedule": []},
                {"name": "Mid", "rate": "0.30", "schedule": []},
                {"name": "High", "rate": "0.40", "schedule": []},
            ],
            "step_tariffs": [
                {"threshold_kwh_per_day": 5.0, "tier_below": "Low", "tier_above": "Mid"},
                {"threshold_kwh_per_day": 10.0, "tier_below": "Mid", "tier_above": "High"},
            ],
        }
    )
    assert len(plan.step_tariffs) == 2

    date = datetime.date(2024, 6, 3)  # Monday
    meter = _make_meter(date, [0.5] * 48)  # 24 kWh uniform

    result = CostCalculator().calculate_period(meter, plan)

    expected_usage = (
        Decimal("5.0") * Decimal("0.20")
        + Decimal("5.0") * Decimal("0.30")
        + Decimal("14.0") * Decimal("0.40")
    )
    assert result.total_usage == expected_usage
    assert result.total_usage == Decimal("8.10")


def test_two_step_tariff_interval_straddles_second_threshold():
    """An interval that crosses the second (higher) step threshold is split correctly.

    Plan:
      - Low  $0.10  catch-all
      - Mid  $0.20  catch-all
      - High $0.30  catch-all
      - step[0]: threshold  4.0, tier_below=Low,  tier_above=Mid
      - step[1]: threshold  6.0, tier_below=Mid,  tier_above=High

    Mon 2024-06-03. Uniform 0.4 kWh per interval.

    After 15 intervals: dct = 6.0 kWh exactly (no straddle of either threshold).
    Check: 4.0 / 0.4 = 10 intervals at Low, then (6.0-4.0) / 0.4 = 5 intervals
    at Mid, 16th interval (idx 15) lands dct at 6.0 exactly → still Mid rate.
    17th interval (idx 16) is first fully in High band.

    Wait — use 0.3 kWh to get a straddle of the second threshold:
      After 20 intervals: dct = 6.0 kWh exactly → interval 21 is High.
      After 13 intervals: dct = 3.9 kWh; interval 14 brings it to 4.2 → straddles step[0].
      After 19 intervals: dct = 5.7 kWh; interval 20 brings it to 6.0 exactly → no straddle.
      After 20 intervals: dct = 6.0; interval 21 → fully in High (above both thresholds).

    Use a non-uniform profile to force the straddle of the SECOND threshold.
    Values: 14 × 0.5 kWh (7.0 kWh total, crosses both thresholds).
      - dct after 8 intervals: 4.0 → interval 9 straddles step[0] (4.0+0.5=4.5 > 4.0)

    Actually, let's use: first 8 intervals 0.5 kWh, last 6 intervals 0.5 kWh.
    Better: use a clean setup where the straddle lands on step[1].

    Profile: 12 × 0.5 kWh = 6.0 kWh, but place them so the second threshold is straddled.
    - 7 intervals × 0.5 = 3.5 kWh (below step[0])
    - Interval 8: 0.5 kWh → dct 3.5 → 4.0 (lands exactly on step[0], no straddle)
    - Interval 9: 0.5 kWh → dct 4.0 → 4.5 (Mid rate)
    - 3 more intervals × 0.5 = 5.0, 5.5, 6.0
    - Use 0.7 kWh for one interval to straddle step[1]:
      After 11 × 0.5 = 5.5; interval 12 = 0.7 → dct 5.5 → 6.2, straddles step[1] (6.0).

    Hand-math:
      i 0..7  (8 × 0.5, dct 0→4.0):  Low  $0.10 → 4.0 × 0.10 = 0.40
      i 8..10 (3 × 0.5, dct 4→5.5):  Mid  $0.20 → 1.5 × 0.20 = 0.30
      i 11    (0.7,     dct 5.5→6.2): split at 6.0 → 0.5 @ Mid $0.20 + 0.2 @ High $0.30
                                        = 0.5×0.20 + 0.2×0.30 = 0.10 + 0.06 = 0.16
      total_usage = 0.40 + 0.30 + 0.16 = 0.86
    """
    plan = ElectricityPlan.model_validate(
        {
            "plan_id": "test_two_step_straddle",
            "retailer": "Test Retailer",
            "plan_name": "Two Step Straddle",
            "daily_supply_charge": "0.00",
            "usage_tiers": [
                {"name": "Low",  "rate": "0.10", "schedule": []},
                {"name": "Mid",  "rate": "0.20", "schedule": []},
                {"name": "High", "rate": "0.30", "schedule": []},
            ],
            "step_tariffs": [
                {"threshold_kwh_per_day": 4.0, "tier_below": "Low", "tier_above": "Mid"},
                {"threshold_kwh_per_day": 6.0, "tier_below": "Mid", "tier_above": "High"},
            ],
        }
    )
    date = datetime.date(2024, 6, 3)  # Monday
    # 8 × 0.5 + 3 × 0.5 + 1 × 0.7 = 4.0 + 1.5 + 0.7 = 6.2 kWh, rest zero
    values = [0.5] * 8 + [0.5] * 3 + [0.7] + [0.0] * 36
    assert len(values) == 48
    meter = _make_meter(date, values)

    result = CostCalculator().calculate_period(meter, plan)

    # Band 0 [0, 4.0):  8 × 0.5 = 4.0 kWh @ Low  $0.10 = 0.40
    # Band 1 [4.0, 6.0):  3 × 0.5 = 1.5 kWh @ Mid $0.20 = 0.30
    # Straddling interval: 0.5 @ Mid $0.20 + 0.2 @ High $0.30 = 0.10 + 0.06 = 0.16
    expected_usage = (
        Decimal("4.0") * Decimal("0.10")
        + Decimal("1.5") * Decimal("0.20")
        + Decimal("0.5") * Decimal("0.20")
        + Decimal("0.2") * Decimal("0.30")
    )
    assert result.total_usage == expected_usage
    assert result.total_usage == Decimal("0.86")


# ── Locale-independent _day_of_week ───────────────────────────────────────────


def test_day_of_week_basic():
    """_day_of_week returns the correct 3-letter abbreviation for Mon, Sat, Sun."""
    ts_mon = pd.Timestamp("2024-06-03", tz=MELBOURNE_TZ)   # Monday
    ts_sat = pd.Timestamp("2024-06-08", tz=MELBOURNE_TZ)   # Saturday
    ts_sun = pd.Timestamp("2024-06-09", tz=MELBOURNE_TZ)   # Sunday

    assert _day_of_week(ts_mon) == "Mon"
    assert _day_of_week(ts_sat) == "Sat"
    assert _day_of_week(ts_sun) == "Sun"


def test_day_of_week_never_calls_strftime(monkeypatch):
    """_day_of_week must not call pd.Timestamp.strftime (locale-dependent).

    Monkeypatching strftime to raise proves the implementation relies only on
    ``pd.Timestamp.weekday()`` (an integer, unaffected by LC_TIME). If the
    production code ever reverts to strftime this test will fail.
    """
    def raising_strftime(self, *args, **kwargs):
        raise RuntimeError(
            "_day_of_week must not call strftime — it is locale-dependent"
        )

    monkeypatch.setattr(pd.Timestamp, "strftime", raising_strftime)

    ts = pd.Timestamp("2024-06-03", tz=MELBOURNE_TZ)  # Monday
    result = _day_of_week(ts)
    assert result == "Mon", f"Expected 'Mon', got {result!r}"


def test_day_of_week_locale_independent():
    """With fr_FR.UTF-8 locale, _day_of_week still returns 'Mon', not 'Lun'.

    Skipped when the French locale is not installed on the test host.
    The strftime-patching test (above) covers the same invariant on every host.
    """
    import locale

    try:
        saved = locale.setlocale(locale.LC_TIME)
        locale.setlocale(locale.LC_TIME, "fr_FR.UTF-8")
    except locale.Error:
        pytest.skip("fr_FR.UTF-8 locale not available on this system")

    try:
        ts = pd.Timestamp("2024-06-03", tz=MELBOURNE_TZ)  # Monday
        result = _day_of_week(ts)
        assert result == "Mon", (
            f"Expected 'Mon' with fr_FR locale (locale-independent), got {result!r}. "
            "Ensure _day_of_week uses weekday(), not strftime."
        )
    finally:
        locale.setlocale(locale.LC_TIME, saved)


# ── Cross-DST step tariff ─────────────────────────────────────────────────────


def test_dst_fall_back_step_tariff_threshold_crossed():
    """Fall-back day (50 intervals): extra kWh from the repeated hour crosses the step threshold.

    On a normal 48-interval day, 48 × 0.1 kWh = 4.8 kWh stays below the
    4.85 kWh/day threshold (all intervals billed at Low $0.20).

    On the fall-back day (50 intervals), the 49th interval (index 48)
    straddles the threshold:
      below_kwh = 4.85 - 4.80 = 0.05 kWh @ Low  $0.20 = $0.010
      above_kwh = 0.10 - 0.05 = 0.05 kWh @ High $0.40 = $0.020
    The 50th interval (index 49) is already above the threshold:
      0.10 kWh @ High $0.40 = $0.040

    Hand-math:
      48 intervals × 0.1 × $0.20  =  $0.960   (below threshold)
      split interval (straddled)   =  $0.030   ($0.010 + $0.020)
      50th interval already_above  =  $0.040
      total_usage                  =  $1.030
      supply                       =  $1.00
      total_net                    =  $2.030

    Contrast: a normal 48-interval day produces 4.8 kWh < 4.85 → all Low,
    total_usage = $0.960, confirming that only the extra fall-back intervals
    push consumption over the threshold.
    """
    plan = ElectricityPlan.model_validate({
        "plan_id": "test_dst_step",
        "retailer": "Test Retailer",
        "plan_name": "DST Step Tariff",
        "daily_supply_charge": "1.00",
        "usage_tiers": [
            {"name": "Low",  "rate": "0.20", "schedule": []},
            {"name": "High", "rate": "0.40", "schedule": []},
        ],
        "step_tariffs": [
            {"threshold_kwh_per_day": 4.85, "tier_below": "Low", "tier_above": "High"}
        ],
    })

    # Fall-back day 2024-04-07 AEDT→AEST: 50 intervals (the 02:00-02:30 hour
    # repeats at UTC+11 then UTC+10).
    idx = pd.date_range("2024-04-07 00:00", "2024-04-07 23:30", freq="30min", tz=MELBOURNE_TZ)
    assert len(idx) == 50, "Precondition: fall-back day must have 50 intervals"
    meter = _meter_from_index(idx, [0.1] * 50)

    result = CostCalculator().calculate_period(meter, plan)

    # 48 full intervals below threshold: 48 × 0.1 × $0.20
    # Interval 49 (index 48) straddles 4.85: 0.05 @ Low + 0.05 @ High
    # Interval 50 (index 49) already above:  0.1 @ High
    expected_usage = (
        Decimal("48") * Decimal("0.1") * Decimal("0.20")   # $0.960
        + Decimal("0.05") * Decimal("0.20")                 # $0.010  (below split)
        + Decimal("0.05") * Decimal("0.40")                 # $0.020  (above split)
        + Decimal("0.1") * Decimal("0.40")                  # $0.040  (already_above)
    )
    assert result.total_usage == expected_usage
    assert result.total_usage == Decimal("1.030")
    assert result.total_supply == Decimal("1.00")
    assert result.total_net == Decimal("2.030")

    # Contrast: normal 48-interval day stays entirely below the threshold
    normal_date = datetime.date(2024, 4, 1)  # a regular non-DST Tuesday
    normal_meter = _make_meter(normal_date, [0.1] * 48)
    normal_result = CostCalculator().calculate_period(normal_meter, plan)
    assert normal_result.total_usage == Decimal("48") * Decimal("0.1") * Decimal("0.20")
    assert normal_result.total_usage == Decimal("0.960"), (
        "Normal 48-interval day must stay below threshold (4.8 < 4.85)"
    )


# ── Negative net cost ─────────────────────────────────────────────────────────


def test_negative_net_cost_high_solar_export():
    """Solar FiT credit exceeds supply + usage → DailyCost.net and total_net are negative.

    A high-export day on a generous FiT rate can produce a negative net
    cost. The calculator must NOT clamp to zero.

    Plan:
      - Supply charge: $0.50/day
      - Flat usage:    $0.30/kWh
      - FiT credit:    $0.12/kWh (exported)

    Day: Mon 2024-06-03, 48 intervals.
      E1 (import): 48 × 0.1 kWh = 4.8 kWh
      B1 (export): 48 × 0.5 kWh = 24.0 kWh

    Hand-math:
      supply =  $0.50
      usage  =  4.8  × $0.30 = $1.44
      credit =  24.0 × $0.12 = $2.88
      net    =  $0.50 + $1.44 − $2.88 = −$0.94  ← must be negative
    """
    plan = ElectricityPlan.model_validate({
        "plan_id": "test_neg_net",
        "retailer": "Test Retailer",
        "plan_name": "High Export FiT",
        "daily_supply_charge": "0.50",
        "usage_tiers": [{"name": "Flat", "rate": "0.30", "schedule": []}],
        "fit_tiers": [{"name": "FiT", "rate": "0.12", "schedule": []}],
    })

    date = datetime.date(2024, 6, 3)
    meter = _make_meter(date, e1_values=[0.1] * 48, b1_values=[0.5] * 48)

    result = CostCalculator().calculate_period(meter, plan)

    expected_supply = Decimal("0.50")
    expected_usage  = Decimal("4.8") * Decimal("0.30")   # = Decimal("1.440")
    expected_credit = Decimal("24.0") * Decimal("0.12")  # = Decimal("2.880")
    expected_net    = expected_supply + expected_usage - expected_credit  # = −0.940

    assert result.total_supply == expected_supply
    assert result.total_usage == expected_usage
    assert result.total_solar_credit == expected_credit
    assert result.total_net == expected_net
    assert result.total_net < Decimal("0"), (
        f"total_net must be negative on a high-export day; got {result.total_net}"
    )

    # Per-day DailyCost.net must also be negative (not clamped to zero)
    assert len(result.daily_costs) == 1
    assert result.daily_costs[0].net < Decimal("0"), (
        f"DailyCost.net must be negative; got {result.daily_costs[0].net}"
    )
    assert result.daily_costs[0].net == expected_net
