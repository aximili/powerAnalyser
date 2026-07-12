"""Tests for the LLM-driven plan extractor.

All tests use MockLLMProvider — no real API calls are made.
"""

from __future__ import annotations

import json

import pytest

from power_analyser.agent.extractors.plan_extractor import PlanExtractor, _extract_json_from_response
from power_analyser.core.tariff.schema import ElectricityPlan

from .conftest import MockLLMProvider, SAMPLE_LLM_RESPONSE, SAMPLE_PAGE_HTML


def test_extract_returns_valid_plans():
    """LLM returning well-formed JSON should produce validated ElectricityPlan objects."""
    provider = MockLLMProvider(response=SAMPLE_LLM_RESPONSE)
    extractor = PlanExtractor(provider)
    plans = extractor.extract_from_text(SAMPLE_PAGE_HTML)

    assert len(plans) == 1
    assert isinstance(plans[0], ElectricityPlan)
    assert plans[0].plan_id == "test_energy_co_basic_flat"
    assert plans[0].retailer == "Test Energy Co"


def test_extract_plan_fields_are_correct():
    provider = MockLLMProvider(response=SAMPLE_LLM_RESPONSE)
    extractor = PlanExtractor(provider)
    plan = extractor.extract_from_text(SAMPLE_PAGE_HTML)[0]

    from decimal import Decimal
    assert plan.daily_supply_charge == Decimal("0.98")
    assert len(plan.usage_tiers) == 1
    assert plan.usage_tiers[0].rate == Decimal("0.28")
    assert len(plan.fit_tiers) == 1


def test_empty_array_response_returns_empty_list():
    provider = MockLLMProvider(response="[]")
    extractor = PlanExtractor(provider)
    plans = extractor.extract_from_text("some page")
    assert plans == []


def test_invalid_json_response_returns_empty_list():
    provider = MockLLMProvider(response="This is not JSON at all.")
    extractor = PlanExtractor(provider)
    plans = extractor.extract_from_text("some page")
    assert plans == []


def test_partial_valid_response_skips_bad_entries():
    """One valid plan + one missing required field → should return only the valid one."""
    response = json.dumps([
        {
            "plan_id": "valid_plan",
            "retailer": "R",
            "plan_name": "P",
            "daily_supply_charge": "1.00",
            "usage_tiers": [{"name": "Flat", "rate": "0.30", "schedule": []}],
        },
        {
            # Missing 'usage_tiers' — will fail validation
            "plan_id": "bad_plan",
            "retailer": "R",
            "plan_name": "P",
            "daily_supply_charge": "1.00",
        },
    ])
    provider = MockLLMProvider(response=response)
    extractor = PlanExtractor(provider)
    plans = extractor.extract_from_text("some page")
    assert len(plans) == 1
    assert plans[0].plan_id == "valid_plan"


def test_markdown_fenced_response_is_handled():
    """LLM sometimes wraps JSON in ```json ... ``` fences."""
    fenced = f"```json\n{SAMPLE_LLM_RESPONSE}\n```"
    provider = MockLLMProvider(response=fenced)
    extractor = PlanExtractor(provider)
    plans = extractor.extract_from_text("page")
    assert len(plans) == 1


def test_extract_json_helper_strips_fences():
    raw = "Here is the data:\n```json\n[1, 2, 3]\n```\nDone."
    assert _extract_json_from_response(raw) == "[1, 2, 3]"


def test_extract_json_helper_returns_empty_on_no_array():
    assert _extract_json_from_response("no array here") == ""


def test_prompt_contains_page_content():
    """Verify the LLM was actually given the page content in the prompt."""
    provider = MockLLMProvider(response="[]")
    extractor = PlanExtractor(provider)
    extractor.extract_from_text("unique_marker_XYZ")
    assert "unique_marker_XYZ" in (provider.last_prompt or "")
