"""Integration test: fall-back DST day through the full pipeline → calculator.

Covers the gap identified in the audit: the pipeline merges 50 raw fall-back
intervals into 48 slots, but the calculator test ``test_dst_fall_back_50_intervals``
uses 50 slots directly (bypassing the pipeline). This test runs the full path:

  1. Synthetic NEM12 CSV in memory with 50 raw intervals for 2024-04-07 (AEDT→AEST).
  2. Load via IngestionPipeline.load() → MeterDataSet (48 merged slots).
  3. Pass to CostCalculator with a flat-rate plan.
  4. Verify: supply charged once, 48 merged intervals contribute to usage,
     total kWh matches the summed (not doubled or dropped) duplicate-hour slots.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from power_analyser.core.ingestion.pipeline import IngestionPipeline
from power_analyser.core.simulation.calculator import CostCalculator
from power_analyser.core.tariff.schema import ElectricityPlan


def _write_fall_back_nem12(tmp_path: Path) -> Path:
    """Write a minimal NEM12 CSV for 2024-04-07 with 50 raw intervals.

    Interval layout (30-min, 50 slots for AEDT→AEST fall-back):
      idx  0–3   : 00:00–01:30 AEDT  → 1.0 kWh each
      idx  4–5   : 02:00–02:30 AEDT (first occurrence)  → 0.3 kWh each
      idx  6–7   : 02:00–02:30 AEST (second occurrence) → 0.2 kWh each
      idx  8–49  : 03:00–23:30 AEST  → 1.0 kWh each

    Pipeline merging (fall-back branch):
      missing_pos = 4, dst_delta = 2
      merged[0] = intervals[4] + intervals[6] = 0.3 + 0.2 = 0.5
      merged[1] = intervals[5] + intervals[7] = 0.3 + 0.2 = 0.5

    Expected 48-slot output:
      4 × 1.0 + 2 × 0.5 + 42 × 1.0 = 47.0 kWh total
    """
    pre = [1.0] * 4           # 00:00–01:30 (indices 0–3)
    first_dup = [0.3, 0.3]    # 02:00 AEDT, 02:30 AEDT (indices 4–5)
    second_dup = [0.2, 0.2]   # 02:00 AEST, 02:30 AEST (indices 6–7)
    post = [1.0] * 42         # 03:00–23:30 (indices 8–49)

    raw_vals = pre + first_dup + second_dup + post
    assert len(raw_vals) == 50, f"Expected 50 raw intervals, got {len(raw_vals)}"

    vals_str = ",".join(f"{v:.1f}" for v in raw_vals)
    csv_text = (
        "100,NEM12,20240408000000,TESTRETAILER,TESTNETWORK\n"
        "200,TESTNMI000001,,,E1,,,kWh,30,20250101\n"
        f"300,20240407,{vals_str},A,0,,20240408000000\n"
        "900\n"
    )

    path = tmp_path / "fall_back_test.csv"
    path.write_text(csv_text, encoding="utf-8")
    return path


def test_fall_back_pipeline_to_calculator(tmp_path):
    """Fall-back NEM12 (50 raw intervals) → pipeline (48 merged) → calculator.

    Hand-math for the merged 48-slot day (flat rate $0.30, supply $1.00):
      Raw slots (50):  pre×4@1.0 | dup1×2@0.3 | dup2×2@0.2 | post×42@1.0
      Pipeline merge:  pre×4@1.0 | merged×2@0.5            | post×42@1.0
      Total kWh:       4.0 + 1.0 + 42.0 = 47.0 kWh
      Usage:           47.0 × $0.30 = $14.10
      Supply:          $1.00 (billed once — one calendar day)
      Net:             $15.10

    Key invariants verified:
      (a) Supply charged exactly once: only one calendar day in the dataset.
      (b) Usage = 47.0 kWh × $0.30 — proves the duplicate-hour intervals
          were SUMMED (not dropped or doubled): dropping first gives 46.6 kWh,
          dropping second gives 46.4 kWh, doubling gives 47.2 kWh.
      (c) len(daily_costs) == 1 confirms the pipeline produced one merged day,
          not two separate dates.
    """
    plan = ElectricityPlan.model_validate({
        "plan_id": "test_flat_integration",
        "retailer": "Test Retailer",
        "plan_name": "Flat Rate Integration",
        "daily_supply_charge": "1.00",
        "usage_tiers": [{"name": "Flat", "rate": "0.30", "schedule": []}],
    })

    csv_path = _write_fall_back_nem12(tmp_path)
    meter = IngestionPipeline().load(csv_path)

    # Pipeline should produce 48 slots (merged from 50 raw)
    assert len(meter.e1) == 48, (
        f"Pipeline should merge 50 fall-back intervals into 48 slots; got {len(meter.e1)}"
    )
    assert meter.start_date == datetime.date(2024, 4, 7)
    assert meter.end_date == datetime.date(2024, 4, 7)

    result = CostCalculator().calculate_period(meter, plan)

    # (a) Supply charged exactly once — one calendar day
    assert result.total_supply == Decimal("1.00")
    assert len(result.daily_costs) == 1
    assert result.daily_costs[0].date == datetime.date(2024, 4, 7)

    # (b) Usage reflects the correctly summed duplicate-hour intervals
    #   merged slot values: 0.3+0.2=0.5 each × 2 slots = 1.0 kWh
    #   regular slots:      1.0 each × 46 slots         = 46.0 kWh
    #   total:              47.0 kWh × $0.30            = $14.10
    expected_total_kwh = Decimal("47.0")
    expected_usage = expected_total_kwh * Decimal("0.30")
    assert result.total_usage == expected_usage, (
        f"Expected total_usage = {expected_usage}; got {result.total_usage}. "
        "Duplicate-hour intervals must be summed (not dropped or doubled)."
    )

    # (c) Net = supply + usage (no export)
    assert result.total_net == Decimal("1.00") + expected_usage

    # Verify the pipeline DST warning was recorded
    assert any("fall-back" in w.lower() or "dst" in w.lower() for w in meter.warnings), (
        f"Expected a DST fall-back warning in meter.warnings; got: {meter.warnings}"
    )
