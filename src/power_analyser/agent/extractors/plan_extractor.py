"""LLM-driven electricity plan extractor.

Sends the page text (or screenshot) to the configured LLM and asks it to
extract electricity plan data in the exact ``ElectricityPlan`` JSON schema.

The LLM response is parsed and validated with pydantic.  Invalid entries
are skipped with a logged warning so one bad plan doesn't abort the whole
extraction.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from power_analyser.core.tariff.schema import ElectricityPlan
from ..llm.base import LLMProvider

logger = logging.getLogger(__name__)

# ── Prompt template ────────────────────────────────────────────────────────────

_EXTRACT_PROMPT = """You are an expert at extracting Australian electricity plan pricing data.

PAGE CONTENT:
{page_content}

Extract ALL electricity plans visible on this page and return them as a JSON array.
Each plan object MUST use this exact schema:
{{
  "plan_id": "unique_snake_case_id",
  "retailer": "Retailer Name",
  "plan_name": "Plan Display Name",
  "daily_supply_charge": "0.9800",   // $/day as decimal string
  "usage_tiers": [
    {{
      "name": "Peak",
      "rate": "0.4100",              // $/kWh as decimal string
      "schedule": [
        {{"days": ["Mon","Tue","Wed","Thu","Fri"], "start": "07:00", "end": "23:00"}}
      ]
    }},
    {{"name": "Off-Peak", "rate": "0.1750", "schedule": []}}
  ],
  "free_windows": [],
  "fit_tiers": [{{"name": "Solar FiT", "rate": "0.0500", "schedule": []}}],
  "step_tariffs": []
}}

IMPORTANT RULES:
- All rates are $/kWh (or $/day for supply charge), as decimal strings.
- Day names: "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"
- Times in 24-hour "HH:MM" format.
- An empty "schedule" list means the tier applies at all times (flat rate).
- If a plan has no solar feed-in tariff, set "fit_tiers": [].
- Generate a unique "plan_id" from retailer + plan name in snake_case.
- Return ONLY a valid JSON array. No explanation, no markdown fences.
- If no electricity plans are visible, return an empty array: []
"""


class PlanExtractor:
    """Extracts ElectricityPlan objects from page content using an LLM."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    def extract_from_text(self, page_text: str) -> list[ElectricityPlan]:
        """Ask the LLM to extract plans from page text content."""
        prompt = _EXTRACT_PROMPT.format(page_content=page_text[:10_000])
        response = self._provider.complete(prompt)
        return self._parse_response(response)

    def extract_from_screenshot(self, screenshot_bytes: bytes, hint_text: str = "") -> list[ElectricityPlan]:
        """Ask the LLM to extract plans from a page screenshot (vision LLM required)."""
        prompt = _EXTRACT_PROMPT.format(
            page_content=f"[Analysing screenshot. Text hint: {hint_text}]"
        )
        response = self._provider.complete_with_image(prompt, screenshot_bytes)
        return self._parse_response(response)

    # ── Private ────────────────────────────────────────────────────────────────

    def _parse_response(self, response: str) -> list[ElectricityPlan]:
        """Parse LLM response into validated ElectricityPlan objects.

        Strips markdown fences, extracts JSON, validates each entry.
        Skips (with a warning) any entries that fail validation.
        """
        raw_json = _extract_json_from_response(response)
        if not raw_json:
            logger.warning("LLM returned no JSON array in response.")
            return []

        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            logger.warning("Could not parse LLM JSON response: %s", exc)
            return []

        if not isinstance(data, list):
            logger.warning("Expected JSON array from LLM, got %s", type(data).__name__)
            return []

        plans: list[ElectricityPlan] = []
        for i, entry in enumerate(data):
            try:
                plans.append(ElectricityPlan.model_validate(entry))
            except Exception as exc:
                logger.warning("Plan entry %d failed validation: %s", i, exc)

        return plans


def _extract_json_from_response(text: str) -> str:
    """Extract the first JSON array from a possibly-fenced LLM response string."""
    # Strip markdown code fences (```json ... ```)
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    text = re.sub(r"```\s*$", "", text).strip()

    # Find the first "[" and last "]" to extract the array
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return ""
    return text[start : end + 1]
