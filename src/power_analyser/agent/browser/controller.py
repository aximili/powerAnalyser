"""Playwright browser controller.

Wraps the Playwright synchronous API into a simple interface used by the
agent orchestrator.  Handles:
  - Browser lifecycle (start / stop)
  - Navigation, clicks, form filling
  - Page content extraction (text + screenshot)
  - CAPTCHA detection (raises CaptchaDetected)

Install Playwright browsers once with:
  playwright install chromium
"""

from __future__ import annotations

import logging
from typing import Optional

from power_analyser.config import Config
from .captcha import CaptchaDetected, is_captcha_present

logger = logging.getLogger(__name__)

# Maximum number of characters of page text to send to the LLM
_MAX_PAGE_TEXT_CHARS = 12_000


class BrowserController:
    """Thin wrapper around a Playwright browser instance."""

    def __init__(self, config: Config) -> None:
        self._headless = config.agent_headless
        self._playwright = None
        self._browser = None
        self._page = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Launch the browser.  Must be called before any other method."""
        from playwright.sync_api import sync_playwright  # type: ignore[import]

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self._headless)
        self._page = self._browser.new_page()
        logger.debug("Browser started (headless=%s)", self._headless)

    def stop(self) -> None:
        """Close the browser and clean up Playwright resources."""
        try:
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception as exc:
            logger.debug("Error during browser teardown: %s", exc)
        finally:
            self._browser = None
            self._playwright = None
            self._page = None

    def __enter__(self) -> "BrowserController":
        self.start()
        return self

    def __exit__(self, *_) -> None:
        self.stop()

    # ── Page interaction ───────────────────────────────────────────────────────

    def navigate(self, url: str, timeout_ms: int = 30_000) -> None:
        """Navigate to ``url`` and check for CAPTCHAs."""
        self._require_page()
        self._page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        self._check_captcha()

    def click(self, selector: str, timeout_ms: int = 10_000) -> None:
        """Click the first element matching ``selector``."""
        self._require_page()
        self._page.click(selector, timeout=timeout_ms)
        self._page.wait_for_load_state("domcontentloaded", timeout=5_000)
        self._check_captcha()

    def fill(self, selector: str, text: str, timeout_ms: int = 5_000) -> None:
        """Clear and fill the form field matching ``selector`` with ``text``."""
        self._require_page()
        self._page.fill(selector, text, timeout=timeout_ms)

    def press_enter(self, selector: str) -> None:
        """Press Enter on the element matching ``selector``."""
        self._require_page()
        self._page.press(selector, "Enter")

    def scroll_to(self, y: int) -> None:
        """Scroll the page to the given vertical pixel position."""
        self._require_page()
        self._page.evaluate(f"window.scrollTo(0, {y})")

    def wait_for_selector(self, selector: str, timeout_ms: int = 10_000) -> None:
        """Block until the element appears in the DOM."""
        self._require_page()
        self._page.wait_for_selector(selector, timeout=timeout_ms)

    # ── Content extraction ─────────────────────────────────────────────────────

    def get_text_content(self) -> str:
        """Return the visible text of the current page, truncated for LLM input."""
        self._require_page()
        try:
            text = self._page.inner_text("body") or ""
        except Exception:
            text = ""
        # Truncate to avoid overwhelming the LLM context window
        return text[:_MAX_PAGE_TEXT_CHARS]

    def get_page_html(self) -> str:
        """Return the outer HTML of the page body (truncated)."""
        self._require_page()
        try:
            html = self._page.inner_html("body") or ""
        except Exception:
            html = ""
        return html[:_MAX_PAGE_TEXT_CHARS]

    def get_screenshot(self) -> bytes:
        """Return a PNG screenshot of the current viewport."""
        self._require_page()
        return self._page.screenshot(type="png")

    def get_current_url(self) -> str:
        self._require_page()
        return self._page.url

    def get_page_title(self) -> str:
        self._require_page()
        return self._page.title()

    # ── CAPTCHA ────────────────────────────────────────────────────────────────

    def _check_captcha(self) -> None:
        """Raise CaptchaDetected if the current page contains a CAPTCHA."""
        if is_captcha_present(self._page):
            raise CaptchaDetected(
                f"CAPTCHA detected on {self.get_current_url()}"
            )

    # ── Internal ───────────────────────────────────────────────────────────────

    def _require_page(self) -> None:
        if self._page is None:
            raise RuntimeError("Browser not started. Call start() first.")
