"""AI agent orchestrator — the observe → reason → act loop.

The orchestrator drives the browser using an LLM as its reasoning engine:
  1. Capture current page content and (optionally) a screenshot.
  2. Build a prompt describing the task, history, and current state.
  3. Ask the LLM what action to take next.
  4. Execute the action via BrowserController.
  5. Repeat until the LLM signals "done" or the iteration limit is reached.

Extraction resilience (the original agent would run ``extract`` once, get 0
plans, and then immediately give up with ``done``):

  * The result of every extraction (including "0 plans") is fed back into the
    action history so the LLM knows whether it actually succeeded.
  * When text extraction yields nothing, the orchestrator automatically
    retries with a page screenshot (vision models can read rendered rates;
    text-only models still get the page text via the screenshot prompt).
  * The agent is not allowed to stop with ``done`` while zero plans have been
    found, until a small number of forced extraction attempts have been made.

CAPTCHA handling:
  When BrowserController raises CaptchaDetected, the orchestrator:
    a. Calls ``on_captcha()`` to notify the GUI.
    b. Blocks on ``_captcha_event.wait()`` until ``signal_captcha_solved()``
       is called (e.g., when the user presses the "Resume" button).
    c. Continues the loop from where it paused.

Thread safety:
  Run ``run()`` in a background thread; call ``signal_captcha_solved()``
  or ``request_stop()`` from the GUI thread.

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

# How many times the agent will force a (screenshot) extraction attempt before
# accepting that no plans can be read and finally allowing a stop.
_MAX_FORCED_EXTRACTS = 3

# ── Agent action prompt ───────────────────────────────────────────────────────

_ACTION_PROMPT = """You are controlling a web browser to {task}.

Current URL: {url}
Page title: {title}
Action history (most recent first):
{history}

Last extraction result: {last_extract}

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
  wait      — pause briefly (no params needed) for a page to finish loading
  extract   — the current page contains plan pricing data; extract it now
  done      — no more useful information can be gathered; stop

CRITICAL RULES:
- "extract" only counts as success if it returns plans. If your last
  extraction returned 0 plans, the rates were NOT read — do NOT choose "done".
  Instead scroll to reveal rates, wait for the page to render, or try
  "extract" again after scrolling.
- Choose "done" ONLY when at least one plan has been extracted, OR you have
  genuinely exhausted every way to find pricing on this site.
- When pricing data is visible, prefer "extract" over every other action.
"""


class AgentOrchestrator:
    """Runs the AI browser agent loop."""

    def __init__(self, provider: LLMProvider, config: Config) -> None:
        self._provider = provider
        self._config = config
        self._extractor = PlanExtractor(provider)
        self._captcha_event = threading.Event()
        self._stop_requested = False

    def signal_captcha_solved(self) -> None:
        """Unblock the agent after the user has solved the CAPTCHA."""
        self._captcha_event.set()

    def request_stop(self) -> None:
        """Ask the running loop to stop after its current action."""
        self._stop_requested = True

    def run(
        self,
        task: str,
        url: str,
        on_captcha: Callable[[], None],
        on_plan_found: Optional[Callable[[ElectricityPlan], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        browser_factory: Optional[Callable[[], BrowserController]] = None,
    ) -> list[ElectricityPlan]:
        """Execute the agent loop and return all extracted plans.

        Args:
            task:           Natural-language description of what to find.
            url:            Starting URL.
            on_captcha:     Called (once) when a CAPTCHA is detected.
            on_plan_found:  Called for each plan as it is extracted.
            on_log:         Called with progress messages for the UI log pane.
            browser_factory: Optional factory used to create the
                ``BrowserController`` (defaults to the real Playwright
                controller).  Injected by tests to avoid launching a browser.
        """
        self._stop_requested = False

        def log(msg: str) -> None:
            logger.info(msg)
            if on_log:
                on_log(msg)

        found_plans: list[ElectricityPlan] = []
        history: list[str] = []
        last_extract = "none yet"
        extract_attempts = 0
        forced_extracts = 0
        max_iter = self._config.max_agent_iterations

        factory = browser_factory or (lambda: BrowserController(self._config))

        with factory() as browser:
            log(f"Navigating to {url}")
            try:
                browser.navigate(url)
            except CaptchaDetected:
                self._handle_captcha(on_captcha, log)
                browser._check_captcha()  # re-check after user solved it

            for iteration in range(max_iter):
                if self._stop_requested:
                    log("Stop requested — halting agent loop.")
                    break

                log(f"Iteration {iteration + 1}/{max_iter}")

                page_text = browser.get_text_content()
                page_url = browser.get_current_url()
                page_title = browser.get_page_title()

                history_summary = "\n".join(history[-5:]) if history else "None"
                prompt = _ACTION_PROMPT.format(
                    task=task,
                    url=page_url,
                    title=page_title,
                    history=history_summary,
                    last_extract=last_extract,
                    page_content=page_text,
                )

                try:
                    raw = self._provider.complete(prompt)
                    action_obj = _parse_action(raw)
                except Exception as exc:
                    log(f"LLM response parse error: {exc}")
                    history.append(f"ERROR parsing action: {exc}")
                    continue

                action = action_obj.get("action", "done")
                params = action_obj.get("params", {}) or {}
                reasoning = action_obj.get("reasoning", "")
                log(f"  Action: {action} — {reasoning}")

                if action == "done":
                    if (
                        not found_plans
                        and forced_extracts < _MAX_FORCED_EXTRACTS
                        and not self._stop_requested
                    ):
                        # The LLM wants to quit, but it has nothing to show for
                        # it. Force one more screenshot extraction before we
                        # truly give up.
                        forced_extracts += 1
                        log(
                            "Agent wants to stop, but 0 plans extracted so far. "
                            f"Forcing a screenshot extraction attempt ({forced_extracts}/"
                            f"{_MAX_FORCED_EXTRACTS}) before giving up…"
                        )
                        new_plans = self._extract_plans(
                            browser, page_text, log, force_screenshot=True
                        )
                        extract_attempts += 1
                        last_extract = _summarise(new_plans)
                        history.append(
                            f"[forced] extract → {len(new_plans)} plan(s) "
                            f"(attempt {extract_attempts})"
                        )
                        if new_plans:
                            self._accept_plans(new_plans, found_plans, on_plan_found, log)
                            continue
                        # Still nothing — loop again so the LLM can reconsider,
                        # unless we've now exhausted forced attempts.
                        continue
                    log("Agent signalled done.")
                    break

                elif action == "extract":
                    extract_attempts += 1
                    new_plans = self._extract_plans(browser, page_text, log)
                    last_extract = _summarise(new_plans)
                    history.append(
                        f"extract → {len(new_plans)} plan(s) (attempt {extract_attempts})"
                    )
                    if not new_plans:
                        history.append(
                            "WARNING: extraction returned 0 plans. The rates may be in an "
                            "image/iframe or not yet rendered — do NOT stop; try scroll/wait "
                            "then extract again."
                        )
                    self._accept_plans(new_plans, found_plans, on_plan_found, log)

                elif action == "navigate":
                    target = params.get("url", "")
                    log(f"  Navigating to {target}")
                    try:
                        browser.navigate(target)
                    except CaptchaDetected:
                        self._handle_captcha(on_captcha, log)
                    history.append(f"navigate → {target}")

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
                        history.append(f"FILL FAILED on {selector!r}: {exc}")

                elif action == "scroll":
                    try:
                        y = int(params.get("y", 800))
                    except (TypeError, ValueError):
                        y = 800
                    browser.scroll_to(y)
                    history.append(f"scroll → {y}px")

                elif action == "wait":
                    import time

                    time.sleep(2)
                    history.append("wait")

                else:
                    log(f"  Unknown action {action!r}; ignoring.")

            else:
                log(f"Reached maximum iterations ({max_iter}). Stopping.")

        log(f"Agent finished. {len(found_plans)} plan(s) extracted.")
        return found_plans

    # ── Private ────────────────────────────────────────────────────────────────

    def _extract_plans(
        self,
        browser: BrowserController,
        page_text: str,
        log: Callable[[str], None],
        force_screenshot: bool = False,
    ) -> list[ElectricityPlan]:
        """Run extraction, falling back to a screenshot when text yields nothing.

        Retailer rate pages are often rendered into images/canvas or behind
        JS, so the visible text may not contain the numbers even though they
        are clearly on screen.  A screenshot gives the (possibly vision) model
        another shot at the same content.
        """
        plans: list[ElectricityPlan] = []

        if not force_screenshot and page_text.strip():
            log("Extracting plan data from page text…")
            plans = self._extractor.extract_from_text(page_text)

        if not plans:
            log("No plans from text — capturing a screenshot and retrying…")
            try:
                screenshot = browser.get_screenshot()
            except Exception as exc:
                log(f"  Could not capture screenshot: {exc}")
                return []
            plans = self._extractor.extract_from_screenshot(screenshot, page_text)

        return plans

    def _accept_plans(
        self,
        new_plans: list[ElectricityPlan],
        found_plans: list[ElectricityPlan],
        on_plan_found: Optional[Callable[[ElectricityPlan], None]],
        log: Callable[[str], None],
    ) -> None:
        for plan in new_plans:
            log(f"  Found plan: {plan.retailer} – {plan.plan_name}")
            found_plans.append(plan)
            if on_plan_found:
                on_plan_found(plan)

    def _handle_captcha(
        self, on_captcha: Callable[[], None], log: Callable[[str], None]
    ) -> None:
        """Pause until the user solves the CAPTCHA."""
        log("⚠  CAPTCHA detected! Waiting for you to solve it…")
        self._captcha_event.clear()
        on_captcha()
        self._captcha_event.wait()  # blocks until signal_captcha_solved() is called
        log("CAPTCHA solved. Resuming.")


def _summarise(plans: list[ElectricityPlan]) -> str:
    """Human-readable one-liner describing the outcome of an extraction."""
    if not plans:
        return "0 plans (FAILED)"
    names = ", ".join(f"{p.retailer} – {p.plan_name}" for p in plans)
    return f"{len(plans)} plan(s): {names}"


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
