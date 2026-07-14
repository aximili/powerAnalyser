"""Tests for the LLM-driven plan extractor.

All tests use MockLLMProvider — no real API calls are made.
"""

from __future__ import annotations

import json

import pytest

from power_analyser.agent.extractors.plan_extractor import (
    PlanExtractor,
    _apply_context,
    _extract_json_from_response,
    _snake_case,
)
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


# ── New: single-object responses, repair pass, screenshot + context ────────────

def _valid_plan_dict(**overrides):
    plan = {
        "plan_id": "test_energy_co_basic_flat",
        "retailer": "Test Energy Co",
        "plan_name": "Basic Flat Plan",
        "daily_supply_charge": "0.9800",
        "usage_tiers": [{"name": "Flat", "rate": "0.2800", "schedule": []}],
        "free_windows": [],
        "fit_tiers": [{"name": "Solar FiT", "rate": "0.0500", "schedule": []}],
        "step_tariffs": [],
    }
    plan.update(overrides)
    return plan


def test_single_object_response_is_wrapped_into_list():
    """A model that returns one ``{...}`` object instead of an array still parses."""
    single = json.dumps(_valid_plan_dict())
    provider = MockLLMProvider(response=single)
    extractor = PlanExtractor(provider)
    plans = extractor.extract_from_text("page")
    assert len(plans) == 1
    assert plans[0].plan_id == "test_energy_co_basic_flat"


def test_extract_json_helper_falls_back_to_single_object():
    assert _extract_json_from_response('prefix {"a": 1} suffix') == '{"a": 1}'
    assert _extract_json_from_response("no json at all") == ""


def test_repair_pass_rescues_an_invalid_entry():
    """First response is invalid; the repair response fixes it."""
    from .conftest import ScriptedLLMProvider

    bad = json.dumps([{"plan_id": "x", "retailer": "R"}])  # missing required fields
    good = json.dumps([_valid_plan_dict()])
    provider = ScriptedLLMProvider(responses=[bad, good])
    extractor = PlanExtractor(provider, max_repair_attempts=1)

    plans = extractor.extract_from_text("page")

    assert len(plans) == 1
    assert plans[0].retailer == "Test Energy Co"
    # The repair prompt should carry the validation error.
    assert any("ERROR" in p for p in provider.prompts)
    assert provider.text_calls == 2  # initial + repair


def test_repair_disabled_when_max_attempts_zero():
    """With repair off, a bad entry is simply dropped (no second call)."""
    from .conftest import ScriptedLLMProvider

    bad = json.dumps([{"plan_id": "x", "retailer": "R"}])
    provider = ScriptedLLMProvider(responses=[bad, "should-not-be-used"])
    extractor = PlanExtractor(provider, max_repair_attempts=0)

    plans = extractor.extract_from_text("page")
    assert plans == []
    assert provider.text_calls == 1


def test_duplicate_plan_ids_are_deduplicated():
    """A repair that echoes an already-valid entry must not double-count it."""
    provider = MockLLMProvider(response=SAMPLE_LLM_RESPONSE)
    extractor = PlanExtractor(provider, max_repair_attempts=1)
    plans = extractor.extract_from_text("page")
    assert len(plans) == 1  # not 2, despite a repair pass returning it again


def test_screenshot_extraction_uses_vision_and_embeds_page_text():
    """complete_with_image is called and the real page text reaches the model."""
    from .conftest import ScriptedLLMProvider

    provider = ScriptedLLMProvider(responses=[SAMPLE_LLM_RESPONSE])
    extractor = PlanExtractor(provider)
    plans = extractor.extract_from_screenshot(b"\x89PNG fake", page_text="DAILY SUPPLY 98c USAGE 28c")

    assert len(plans) == 1
    assert provider.image_calls == 1
    assert provider.text_calls == 0
    assert "DAILY SUPPLY 98c USAGE 28c" in provider.prompts[0]


def test_context_extraction_forces_retailer_and_plan_name():
    """User-supplied identity always wins and plan_id is regenerated."""
    provider = MockLLMProvider(response=SAMPLE_LLM_RESPONSE)
    extractor = PlanExtractor(provider)
    plans = extractor.extract_from_screenshot_with_context(
        b"\x89PNG fake", retailer="Amber", plan_name="Smart Plan"
    )
    assert len(plans) == 1
    plan = plans[0]
    assert plan.retailer == "Amber"
    assert plan.plan_name == "Smart Plan"
    assert plan.plan_id == "amber_smart_plan"
    assert "Amber" in (provider.last_prompt or "")


def test_context_extraction_embeds_page_text():
    """Text extracted from a PDF is embedded so text-only models can read it."""
    provider = MockLLMProvider(response=SAMPLE_LLM_RESPONSE)
    extractor = PlanExtractor(provider)
    pdf_text = "DAILY SUPPLY CHARGE 98.0c/day\nUSAGE 28.0c/kWh (all times)"
    extractor.extract_from_screenshot_with_context(
        b"\x89PNG fake", retailer="Amber", plan_name="Smart Plan", page_text=pdf_text
    )

    prompt = provider.last_prompt or ""
    assert pdf_text in prompt  # the document text reached the model
    assert "Amber" in prompt   # context is still applied alongside the text


def test_context_extraction_omits_text_block_when_no_page_text():
    """No page_text → no document text block in the prompt (back-compat)."""
    provider = MockLLMProvider(response=SAMPLE_LLM_RESPONSE)
    extractor = PlanExtractor(provider)
    extractor.extract_from_screenshot_with_context(
        b"\x89PNG fake", retailer="Amber", plan_name="Smart Plan"
    )
    prompt = provider.last_prompt or ""
    assert "TEXT CONTENT EXTRACTED FROM THE DOCUMENT" not in prompt


def test_apply_context_helper():
    plan = ElectricityPlan.model_validate(_valid_plan_dict())
    _apply_context(plan, "  Red Energy  ", "")
    assert plan.retailer == "Red Energy"
    assert plan.plan_id == "red_energy_basic_flat_plan"


def test_snake_case_helper():
    assert _snake_case("Amber Electric - Smart Plan!") == "amber_electric_smart_plan"
    assert _snake_case("") == "plan"


# ── last_updated auto-stamping + conditions passthrough ────────────────────────


def test_extracted_plan_gets_last_updated_stamp():
    """The code (not the LLM) stamps last_updated as an ISO-8601 string."""
    provider = MockLLMProvider(response=SAMPLE_LLM_RESPONSE)
    extractor = PlanExtractor(provider)
    plan = extractor.extract_from_text("page")[0]

    assert plan.last_updated is not None
    # ISO-8601 with a timezone offset, e.g. 2026-07-14T11:30:00+10:00
    from datetime import datetime
    parsed = datetime.fromisoformat(plan.last_updated)
    assert parsed.tzinfo is not None


def test_last_updated_is_consistent_within_one_call():
    """All plans captured together share the same capture timestamp."""
    two = json.dumps([_valid_plan_dict(), _valid_plan_dict(plan_id="second_plan")])
    provider = MockLLMProvider(response=two)
    extractor = PlanExtractor(provider)
    plans = extractor.extract_from_text("page")
    assert len(plans) == 2
    assert plans[0].last_updated == plans[1].last_updated


def test_conditions_from_llm_response_are_preserved():
    """Eligibility conditions emitted by the model pass straight through."""
    payload = json.dumps(
        [_valid_plan_dict(conditions=["Direct debit required", "Pay-on-time discount included"])]
    )
    provider = MockLLMProvider(response=payload)
    extractor = PlanExtractor(provider)
    plan = extractor.extract_from_text("page")[0]

    assert plan.conditions == ["Direct debit required", "Pay-on-time discount included"]

