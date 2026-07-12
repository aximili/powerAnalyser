"""CAPTCHA detection and pause/resume signalling.

When the browser encounters a CAPTCHA, it raises ``CaptchaDetected``.
The orchestrator catches this, calls the ``on_captcha`` callback to notify
the GUI or CLI, then waits on a ``threading.Event`` until the user signals
that the CAPTCHA has been solved.
"""

from __future__ import annotations

# Known CAPTCHA-serving iframe src patterns
CAPTCHA_SRC_PATTERNS = [
    "recaptcha",
    "hcaptcha",
    "cloudflare",
    "turnstile",
    "arkoselabs",
    "funcaptcha",
    "geetest",
    "challenge-platform",
]

# Common CAPTCHA container CSS selectors
CAPTCHA_SELECTORS = [
    ".g-recaptcha",
    ".h-captcha",
    "#captcha",
    "[id*=captcha]",
    "[class*=captcha]",
    "[id*=challenge]",
    ".cf-challenge-container",
]

# Page text indicators (lower-cased)
CAPTCHA_TEXT_HINTS = [
    "verify you are human",
    "complete the captcha",
    "prove you're not a robot",
    "solve the challenge",
    "access denied",
    "checking your browser",
]


class CaptchaDetected(Exception):
    """Raised when the browser detects a CAPTCHA challenge."""


def is_captcha_present(page) -> bool:  # type: ignore[type-arg]
    """Return True if the Playwright ``page`` appears to have a CAPTCHA.

    Checks iframe sources, known CSS selectors, and page body text.
    """
    try:
        # Check iframes for known CAPTCHA CDN patterns
        for iframe in page.frames[1:]:  # skip main frame
            src = iframe.url.lower()
            if any(p in src for p in CAPTCHA_SRC_PATTERNS):
                return True

        # Check for known CAPTCHA container elements
        for selector in CAPTCHA_SELECTORS:
            if page.query_selector(selector):
                return True

        # Check visible page text for hints
        body_text = (page.inner_text("body") or "").lower()
        if any(hint in body_text for hint in CAPTCHA_TEXT_HINTS):
            return True

    except Exception:
        pass

    return False
