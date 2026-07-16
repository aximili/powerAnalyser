"""Tests for the tariff schema — pydantic validation coverage."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from power_analyser.core.tariff.schema import ElectricityPlan
from power_analyser.core.tariff.loader import load_plan, load_plans_dir, save_plan

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


# ── save_plan upsert (data/plans persistence) ─────────────────────────────────


def test_save_plan_writes_json_that_round_trips(tmp_path, flat_rate_plan_dict):
    """A saved plan reloads to an equal ElectricityPlan."""
    plan = ElectricityPlan.model_validate(flat_rate_plan_dict)
    path = save_plan(plan, directory=tmp_path)

    assert path.exists()
    assert path.name == f"{plan.plan_id}.json"
    reloaded = load_plan(path)
    assert reloaded.plan_id == plan.plan_id
    assert reloaded.retailer == plan.retailer
    assert reloaded.daily_supply_charge == plan.daily_supply_charge


def test_save_plan_upserts_by_plan_id(tmp_path, flat_rate_plan_dict):
    """Saving with the same plan_id overwrites the existing file."""
    plan = ElectricityPlan.model_validate(flat_rate_plan_dict)
    save_plan(plan, directory=tmp_path)

    # Change the rate and re-save with the same plan_id.
    from decimal import Decimal
    modified = plan.model_copy(update={"daily_supply_charge": Decimal("2.50")})
    path = save_plan(modified, directory=tmp_path)

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1  # still exactly one file — overwritten, not duplicated
    reloaded = load_plan(path)
    assert reloaded.daily_supply_charge == Decimal("2.50")


def test_save_plan_preserves_decimal_as_string(tmp_path, flat_rate_plan_dict):
    """Decimal fields must be written as JSON strings to preserve precision."""
    plan = ElectricityPlan.model_validate(flat_rate_plan_dict)
    path = save_plan(plan, directory=tmp_path)
    raw = path.read_text(encoding="utf-8")
    # Decimal fields are rendered as quoted JSON strings, matching the
    # hand-authored sample files in data/plans/.
    assert '"daily_supply_charge": "1.00"' in raw
    assert '"rate": "0.30"' in raw


def test_save_plan_defaults_to_configured_data_dir(tmp_path, monkeypatch, flat_rate_plan_dict):
    """With no directory given, save_plan writes to <data_dir>/plans/."""
    from power_analyser import config as cfg_module

    fake_config = cfg_module.Config()
    fake_config.data_dir = tmp_path
    monkeypatch.setattr(cfg_module, "get_config", lambda: fake_config)

    plan = ElectricityPlan.model_validate(flat_rate_plan_dict)
    path = save_plan(plan)

    assert path == tmp_path / "plans" / f"{plan.plan_id}.json"
    assert path.exists()
