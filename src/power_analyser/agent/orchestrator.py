"""AI agent orchestrator — the observe → reason → act loop.

The orchestrator drives the browser using an LLM as its reasoning engine:
  1. Capture current page content and (optionally) a screenshot.
  2. Build a prompt describing the task, history, and current state.
  3. Ask the LLM what action to take next.
  4. Execute the action via BrowserController.
  5. Repeat until the LLM signals "done" or the iteration limit is reached.

CAPTCHA handling:
  When BrowserController raises CaptchaDetected, the orchestrator:
    a. Calls ``on_captcha()`` to notify the GUI.
    b. Blocks on ``_captcha_event.wait()`` until ``signal_captcha_solved()``
       is called (e.g., when the user presses the "Resume" button).
    c. Continues the loop from where it paused.

Thread safety:
  Run ``run()`` in a background thread; call ``signal_captcha_solved()``
  from the GUI thread to unblock it.

Usage:
  orchestrator = AgentOrchestrator(provider, config)
  plans = orchestrator.run(
      task="Find all electricity plans on this page",
      url="https://example.com/plans",
      on_captcha=lambda: gui.show_captcha_banner(),
      on_log=lambda msg: gui.append_log(msg),
  )
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Callable, Optional

from power_analyser.config import Config
from power_analyser.core.tariff.schema import ElectricityPlan
from .browser.captcha import CaptchaDetected
from .browser.controller import BrowserController
from .extractors.plan_extractor import PlanExtractor
from .llm.base import LLMProvider

logger = logging.getLogger(__name__)

# ── Agent action prompt ───────────────────────────────────────────────────────

_ACTION_PROMPT = """You are controlling a web browser to {task}.

Current URL: {url}
Page title: {title}
Action history (most recent first):
{history}

Current page content (truncated):
{page_content}

Choose the next action. Reply with ONLY a JSON object using this schema:
{{
  "reasoning": "One sentence explaining what you see and why you're taking this action.",
  "action": "one of: navigate | click | fill | scroll | wait | extract | done",
  "params": {{
    "url":      "...",      // for navigate
    "selector": "...",      // CSS selector for click or fill
    "text":     "...",      // text to type for fill
    "y":        500         // pixel offset for scroll
  }}
}}

Action guide:
  navigate  — go to a different URL
  click     — click a button, link, or element
  fill      — type text into an input field
  scroll    — scroll the page down (use y: 800 to go down ~one screen)
  wait      — pause briefly (no params needed)
  extract   — the current page contains plan pricing data; extract it now
  done      — no more useful information can be gathered; stop

When in doubt, prefer "extract" over "done" if pricing data is visible.
"""


class AgentOrchestrator:
    """Runs the AI browser agent loop."""

    def __init__(self, provider: LLMProvider, config: Config) -> None:
        self._provider = provider
        self._config = config
        self._extractor = PlanExtractor(provider)
        self._captcha_event = threading.Event()

    def signal_captcha_solved(self) -> None:
        """Unblock the agent after the user has solved the CAPTCHA."""
        self._captcha_event.set()

    def run(
        self,
        task: str,
        url: str,
        on_captcha: Callable[[], None],
        on_plan_found: Optional[Callable[[ElectricityPlan], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> list[ElectricityPlan]:
        """Execute the agent loop and return all extracted plans.

        Args:
            task:         Natural-language description of what to find.
            url:          Starting URL.
            on_captcha:   Called (once) when a CAPTCHA is detected.
            on_plan_found: Called for each plan as it is extracted.
            on_log:       Called with progress messages for the UI log pane.
        """
        def log(msg: str) -> None:
            logger.info(msg)
            if on_log:
                on_log(msg)

        found_plans: list[ElectricityPlan] = []
        history: list[str] = []
        max_iter = self._config.max_agent_iterations

        with BrowserController(self._config) as browser:
            log(f"Navigating to {url}")
            try:
                browser.navigate(url)
            except CaptchaDetected:
                self._handle_captcha(on_captcha, log)
                browser._check_captcha()  # re-check after user solved it

            for iteration in range(max_iter):
                log(f"Iteration {iteration + 1}/{max_iter}")

                page_text = browser.get_text_content()
                page_url = browser.get_current_url()
                page_title = browser.get_page_title()

                # Build prompt with last 5 actions as history
                history_summary = "\n".join(history[-5:]) if history else "None"
                prompt = _ACTION_PROMPT.format(
                    task=task,
                    url=page_url,
                    title=page_title,
                    history=history_summary,
                    page_content=page_text,
                )

                # Ask LLM what to do next
                try:
                    raw = self._provider.complete(prompt)
                    action_obj = _parse_action(raw)
                except Exception as exc:
                    log(f"LLM response parse error: {exc}")
                    history.append(f"ERROR: {exc}")
                    continue

                action = action_obj.get("action", "done")
                params = action_obj.get("params", {})
                reasoning = action_obj.get("reasoning", "")
                log(f"  Action: {action} — {reasoning}")
                history.append(f"[{iteration + 1}] {action}: {reasoning}")

                if action == "done":
                    log("Agent signalled done.")
                    break

                elif action == "extract":
                    log("Extracting plan data from current page…")
                    screenshot = None
                    if not page_text.strip():
                        screenshot = browser.get_screenshot()
                    new_plans = (
                        self._extractor.extract_from_screenshot(screenshot, page_text)
                        if screenshot
                        else self._extractor.extract_from_text(page_text)
                    )
                    for plan in new_plans:
                        log(f"  Found plan: {plan.retailer} – {plan.plan_name}")
                        found_plans.append(plan)
                        if on_plan_found:
                            on_plan_found(plan)

                elif action == "navigate":
                    target = params.get("url", "")
                    log(f"  Navigating to {target}")
                    try:
                        browser.navigate(target)
                    except CaptchaDetected:
                        self._handle_captcha(on_captcha, log)

                elif action == "click":
                    selector = params.get("selector", "")
                    log(f"  Clicking {selector!r}")
                    try:
                        browser.click(selector)
                    except CaptchaDetected:
                        self._handle_captcha(on_captcha, log)
                    except Exception as exc:
                        log(f"  Click failed: {exc}")
                        history.append(f"CLICK FAILED on {selector!r}: {exc}")

                elif action == "fill":
                    selector = params.get("selector", "")
                    text = params.get("text", "")
                    log(f"  Filling {selector!r} with {text!r}")
                    try:
                        browser.fill(selector, text)
                    except Exception as exc:
                        log(f"  Fill failed: {exc}")

                elif action == "scroll":
                    y = int(params.get("y", 800))
                    browser.scroll_to(y)

                elif action == "wait":
                    import time
                    time.sleep(2)

            else:
                log(f"Reached maximum iterations ({max_iter}). Stopping.")

        log(f"Agent finished. {len(found_plans)} plan(s) extracted.")
        return found_plans

    # ── Private ────────────────────────────────────────────────────────────────

    def _handle_captcha(
        self, on_captcha: Callable[[], None], log: Callable[[str], None]
    ) -> None:
        """Pause until the user solves the CAPTCHA."""
        log("⚠  CAPTCHA detected! Waiting for you to solve it…")
        self._captcha_event.clear()
        on_captcha()
        self._captcha_event.wait()  # blocks until signal_captcha_solved() is called
        log("CAPTCHA solved. Resuming.")


def _parse_action(raw: str) -> dict:
    """Extract the first JSON object from the LLM response."""
    import re

    raw = re.sub(r"```(?:json)?\s*", "", raw).strip()
    raw = re.sub(r"```\s*$", "", raw).strip()

    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in LLM response")
    return json.loads(raw[start : end + 1])
