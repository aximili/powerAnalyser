"""Tests for the tariff schema — pydantic validation coverage."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from power_analyser.core.tariff.schema import ElectricityPlan
from power_analyser.core.tariff.loader import load_plan, load_plans_dir

from .conftest import PLANS_DIR, flat_rate_plan_dict, tou_plan_dict


def test_valid_flat_rate_plan(flat_rate_plan_dict):
    plan = ElectricityPlan.model_validate(flat_rate_plan_dict)
    assert plan.plan_id == "test_flat"
    assert len(plan.usage_tiers) == 1


def test_optional_fields_default_when_omitted(flat_rate_plan_dict):
    """last_updated / conditions / valid_from are optional with sane defaults."""
    plan = ElectricityPlan.model_validate(flat_rate_plan_dict)
    assert plan.last_updated is None
    assert plan.conditions == []
    assert plan.valid_from is None


def test_last_updated_and_conditions_round_trip():
    plan = ElectricityPlan.model_validate(
        {
            "plan_id": "x", "retailer": "R", "plan_name": "P",
            "daily_supply_charge": "1.0",
            "last_updated": "2026-07-14T11:30:00+10:00",
            "conditions": ["Direct debit required", "Pay-on-time discount included"],
            "usage_tiers": [{"name": "Flat", "rate": "0.30", "schedule": []}],
        }
    )
    assert plan.last_updated == "2026-07-14T11:30:00+10:00"
    assert plan.conditions == ["Direct debit required", "Pay-on-time discount included"]


def test_valid_tou_plan(tou_plan_dict):
    plan = ElectricityPlan.model_validate(tou_plan_dict)
    assert len(plan.usage_tiers) == 2


def test_missing_usage_tiers_raises():
    with pytest.raises(ValidationError):
        ElectricityPlan.model_validate(
            {"plan_id": "x", "retailer": "R", "plan_name": "P",
             "daily_supply_charge": "1.0", "usage_tiers": []}
        )


def test_negative_rate_raises():
    with pytest.raises(ValidationError):
        ElectricityPlan.model_validate(
            {"plan_id": "x", "retailer": "R", "plan_name": "P",
             "daily_supply_charge": "1.0",
             "usage_tiers": [{"name": "T", "rate": "-0.10", "schedule": []}]}
        )


def test_free_window_unknown_overflow_tier_raises():
    with pytest.raises(ValueError, match="unknown overflow_tier"):
        ElectricityPlan.model_validate(
            {
                "plan_id": "x", "retailer": "R", "plan_name": "P",
                "daily_supply_charge": "1.0",
                "usage_tiers": [{"name": "Standard", "rate": "0.30", "schedule": []}],
                "free_windows": [
                    {
                        "name": "FW",
                        "schedule": [{"days": ["Mon"], "start": "11:00", "end": "14:00"}],
                        "fair_use_cap_kwh": 1.0,
                        "overflow_tier": "NONEXISTENT",
                    }
                ],
            }
        )


def test_step_tariff_unknown_tier_raises():
    with pytest.raises(ValueError, match="unknown tier_below"):
        ElectricityPlan.model_validate(
            {
                "plan_id": "x", "retailer": "R", "plan_name": "P",
                "daily_supply_charge": "1.0",
                "usage_tiers": [{"name": "Standard", "rate": "0.30", "schedule": []}],
                "step_tariffs": [
                    {"threshold_kwh_per_day": 10.0, "tier_below": "MISSING", "tier_above": "Standard"}
                ],
            }
        )


def test_load_all_sample_plans():
    plans = load_plans_dir(PLANS_DIR)
    assert len(plans) == 3, f"Expected 3 sample plans, got {len(plans)}"


def test_sample_plans_have_unique_ids():
    plans = load_plans_dir(PLANS_DIR)
    ids = [p.plan_id for p in plans]
    assert len(ids) == len(set(ids)), "Plan IDs must be unique"


def test_smart_rate_plan_has_free_window():
    plans = load_plans_dir(PLANS_DIR)
    smart = next((p for p in plans if p.plan_id == "sample_smart_rate"), None)
    assert smart is not None
    assert len(smart.free_windows) == 1
    assert smart.free_windows[0].name == "Midday Power Saver"
    assert smart.free_windows[0].fair_use_cap_kwh == 2.0
