"""Tests for the agent orchestrator loop.

These run without Playwright by injecting a :class:`FakeBrowser` via the
``browser_factory`` parameter of :meth:`AgentOrchestrator.run`.  A
:class:`ScriptedLLMProvider` returns successive responses (both action JSON
and extraction JSON) so we can drive the observe→reason→act loop deterministically.
"""

from __future__ import annotations

import json
from typing import Optional

from power_analyser.agent.orchestrator import AgentOrchestrator
from power_analyser.config import Config
from power_analyser.core.tariff.schema import ElectricityPlan

from .conftest import SAMPLE_LLM_RESPONSE, ScriptedLLMProvider

_ACTION_EXTRACT = '{"action":"extract","reasoning":"rates visible","params":{}}'
_ACTION_DONE = '{"action":"done","reasoning":"finished","params":{}}'
_ACTION_FLY = '{"action":"fly","reasoning":"not a real action","params":{}}'


class FakeBrowser:
    """Minimal stand-in for ``BrowserController`` used by the orchestrator."""

    def __init__(self) -> None:
        self.page_text = "Estimated yearly price Daily supply 98c Usage 28c/kWh"
        self.url = "https://example.com/plans"
        self.screenshots = 0

    # context manager
    def __enter__(self) -> "FakeBrowser":
        return self

    def __exit__(self, *_args) -> None:
        return None

    # interface used by AgentOrchestrator.run
    def navigate(self, url: str) -> None:
        self.url = url

    def _check_captcha(self) -> None:  # pragma: no cover - never raised here
        return None

    def get_text_content(self) -> str:
        return self.page_text

    def get_current_url(self) -> str:
        return self.url

    def get_page_title(self) -> str:
        return "Plans"

    def get_screenshot(self) -> bytes:
        self.screenshots += 1
        return b"\x89PNG\r\n\x1a\n fake-screenshot"

    def click(self, selector: str) -> None:
        pass

    def fill(self, selector: str, text: str) -> None:
        pass

    def scroll_to(self, y: int) -> None:
        pass


def _run(provider, config, logs):
    orchestrator = AgentOrchestrator(provider, config)
    plans_found: list[ElectricityPlan] = []
    return orchestrator.run(
        task="find plans",
        url="https://example.com/plans",
        on_captcha=lambda: None,
        on_plan_found=plans_found.append,
        on_log=logs.append,
        browser_factory=FakeBrowser,
    )


def test_zero_plans_triggers_screenshot_fallback_and_blocks_premature_done():
    """The exact bug: extract returns 0, then the agent must NOT give up.

    Sequence of LLM responses:
      1. action: extract
      2. text extraction -> []        (text yields nothing)
      3. screenshot extraction -> []  (auto fallback also empty)
      4. action: done                 (agent tries to quit with 0 plans)
      5. FORCED screenshot extraction -> a real plan
      6. action: done                 (now allowed, since a plan exists)
    """
    provider = ScriptedLLMProvider(
        responses=[
            _ACTION_EXTRACT,
            "[]",          # text extraction: nothing
            "[]",          # screenshot fallback: nothing
            _ACTION_DONE,  # premature done -> overridden
            SAMPLE_LLM_RESPONSE,  # forced screenshot extraction succeeds
            _ACTION_DONE,  # genuine done
        ]
    )
    config = Config(max_agent_iterations=10)
    logs: list[str] = []

    plans = _run(provider, config, logs)

    assert len(plans) == 1
    assert plans[0].retailer == "Test Energy Co"
    # A screenshot-based extraction must have happened at least once.
    assert provider.image_calls >= 1
    # And the orchestrator logged that it refused to stop empty-handed.
    joined = "\n".join(logs).lower()
    assert "forcing" in joined


def test_successful_text_extraction_then_done():
    """Happy path: text extraction yields a plan, then the agent stops."""
    provider = ScriptedLLMProvider(
        responses=[_ACTION_EXTRACT, SAMPLE_LLM_RESPONSE, _ACTION_DONE]
    )
    config = Config(max_agent_iterations=5)
    logs: list[str] = []

    plans = _run(provider, config, logs)

    assert len(plans) == 1
    assert provider.image_calls == 0  # text worked, no screenshot needed


def test_unknown_action_is_ignored_without_crash():
    provider = ScriptedLLMProvider(
        responses=[
            _ACTION_FLY,        # ignored
            _ACTION_EXTRACT,    # then a real extract
            SAMPLE_LLM_RESPONSE,
            _ACTION_DONE,
        ]
    )
    config = Config(max_agent_iterations=6)
    plans = _run(provider, config, [])
    assert len(plans) == 1
