"""Shared fixtures for agent tests.

All agent tests must run without network access and without a real LLM.
MockLLMProvider returns deterministic responses for given inputs.
"""

from __future__ import annotations

from typing import Optional

import pytest

from power_analyser.agent.llm.base import LLMProvider


class MockLLMProvider(LLMProvider):
    """Returns a fixed text response, regardless of the prompt.

    Set ``response`` before calling ``complete`` to control what comes back.
    """

    def __init__(self, response: str = "") -> None:
        self.response = response
        self.last_prompt: Optional[str] = None

    def complete(self, prompt: str) -> str:
        self.last_prompt = prompt
        return self.response

    def complete_with_image(self, prompt: str, image_bytes: bytes) -> str:
        self.last_prompt = prompt
        return self.response


class ScriptedLLMProvider(LLMProvider):
    """Returns successive responses from a list, one per call.

    Useful for testing the extractor's repair pass and the orchestrator loop,
    where the model's behaviour changes between calls.  Records every prompt
    in ``prompts`` and tracks whether a vision (image) call was made.
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []
        self.image_calls = 0
        self.text_calls = 0

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.text_calls += 1
        return self._responses.pop(0) if self._responses else ""

    def complete_with_image(self, prompt: str, image_bytes: bytes) -> str:
        self.prompts.append(prompt)
        self.image_calls += 1
        return self._responses.pop(0) if self._responses else ""


@pytest.fixture
def mock_provider():
    return MockLLMProvider()


# ── Static HTML fixture used by extractor tests ────────────────────────────────

SAMPLE_PAGE_HTML = """
<html><body>
<h1>Our Electricity Plans</h1>
<div class="plan">
  <h2>Basic Flat Plan</h2>
  <p>Retailer: Test Energy Co</p>
  <p>Daily Supply Charge: 98c/day ($0.98/day)</p>
  <p>Usage: 28c/kWh (all times, all days)</p>
  <p>Solar Feed-in: 5c/kWh</p>
</div>
</body></html>
"""

# The LLM response we'll simulate for the above page
SAMPLE_LLM_RESPONSE = """[
  {
    "plan_id": "test_energy_co_basic_flat",
    "retailer": "Test Energy Co",
    "plan_name": "Basic Flat Plan",
    "daily_supply_charge": "0.9800",
    "usage_tiers": [
      {"name": "Flat", "rate": "0.2800", "schedule": []}
    ],
    "free_windows": [],
    "fit_tiers": [
      {"name": "Solar FiT", "rate": "0.0500", "schedule": []}
    ],
    "step_tariffs": []
  }
]"""
