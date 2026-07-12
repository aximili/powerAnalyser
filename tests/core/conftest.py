"""Shared fixtures for core engine tests."""

from __future__ import annotations

import datetime
from decimal import Decimal
from pathlib import Path

import pytest

# ── Paths ──────────────────────────────────────────────────────────────────────

SAMPLE_NEM12 = Path(__file__).parents[2] / "data" / "sample_nem12.csv"
PLANS_DIR = Path(__file__).parents[2] / "data" / "plans"


# ── Minimal plan dicts (used to build ElectricityPlan objects in tests) ────────

@pytest.fixture
def flat_rate_plan_dict():
    return {
        "plan_id": "test_flat",
        "retailer": "Test Retailer",
        "plan_name": "Flat Rate Test",
        "daily_supply_charge": "1.00",
        "usage_tiers": [
            {"name": "Flat", "rate": "0.30", "schedule": []}
        ],
    }


@pytest.fixture
def tou_plan_dict():
    """Peak Mon-Fri 07:00-23:00 at $0.40; Off-Peak everything else at $0.20."""
    return {
        "plan_id": "test_tou",
        "retailer": "Test Retailer",
        "plan_name": "ToU Test",
        "daily_supply_charge": "1.00",
        "usage_tiers": [
            {
                "name": "Peak",
                "rate": "0.40",
                "schedule": [
                    {"days": ["Mon", "Tue", "Wed", "Thu", "Fri"], "start": "07:00", "end": "23:00"}
                ],
            },
            {"name": "Off-Peak", "rate": "0.20", "schedule": []},
        ],
    }


@pytest.fixture
def free_window_plan_dict():
    """Flat rate + free midday window (11:00-14:00) capped at 1.5 kWh/day."""
    return {
        "plan_id": "test_fw",
        "retailer": "Test Retailer",
        "plan_name": "Free Window Test",
        "daily_supply_charge": "1.00",
        "usage_tiers": [
            {"name": "Standard", "rate": "0.30", "schedule": []}
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
                "overflow_tier": "Standard",
            }
        ],
    }


@pytest.fixture
def step_tariff_plan_dict():
    """Flat rate with step: first 5 kWh/day at $0.20, above at $0.40."""
    return {
        "plan_id": "test_step",
        "retailer": "Test Retailer",
        "plan_name": "Step Tariff Test",
        "daily_supply_charge": "1.00",
        "usage_tiers": [
            {"name": "Low", "rate": "0.20", "schedule": []},
            {"name": "High", "rate": "0.40", "schedule": []},
        ],
        "step_tariffs": [
            {"threshold_kwh_per_day": 5.0, "tier_below": "Low", "tier_above": "High"}
        ],
    }
