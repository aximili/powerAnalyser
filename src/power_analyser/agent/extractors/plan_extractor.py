"""LLM-driven electricity plan extractor.

Sends the page text (or screenshot) to the configured LLM and asks it to
extract electricity plan data in the exact ``ElectricityPlan`` JSON schema.

Robustness features (important for smaller local models that frequently emit
malformed JSON):

  * Single-object responses are wrapped into a one-element array.
  * Markdown code fences are stripped before parsing.
  * Entries that fail pydantic validation are sent back to the LLM in a
    *repair pass* together with the validation errors.  The number of repair
    rounds is bounded by ``max_repair_attempts`` (default 1).
  * Screenshot extraction always embeds the page text too, so a text-only
    model that ignores the image still has something to work with.

The extractor is independent of the orchestrator and can be called directly:

    >>> from power_analyser.agent.extractors.plan_extractor import PlanExtractor
    >>> from power_analyser.agent.llm.ollama_provider import OllamaProvider
    >>> provider = OllamaProvider.__new__(OllamaProvider)  # (illustrative)
    >>> extractor = PlanExtractor(provider)
    >>> plans = extractor.extract_from_text(page_html)
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime
from typing import Any, Optional

from power_analyser.core.tariff.schema import ElectricityPlan
from ..llm.base import LLMProvider

logger = logging.getLogger(__name__)

# Tags replaced into the prompt templates (avoids ``str.format`` brace-escaping
# the entire JSON schema, which is bug-prone).
_CONTENT_TAG = "{PAGE_CONTENT}"
_CONTEXT_TAG = "{CONTEXT}"
_FAILURES_TAG = "{FAILURES}"

# Maximum characters of page text sent to the LLM in a single prompt.
_MAX_CONTENT_CHARS = 10_000

# ── Shared schema block (natural JSON — no brace doubling needed) ───────────────

_SCHEMA_BLOCK = """{
  "plan_id": "unique_snake_case_id",
  "retailer": "Retailer Name",
  "plan_name": "Plan Display Name",
  "daily_supply_charge": "0.9800",            // $/day as a decimal STRING
  "usage_tiers": [
    {
      "name": "Peak",
      "rate": "0.4100",                        // $/kWh as a decimal STRING
      "schedule": [
        {"days": ["Mon","Tue","Wed","Thu","Fri"], "start": "07:00", "end": "23:00"}
      ]
    },
    {"name": "Off-Peak", "rate": "0.1750", "schedule": []}
  ],
  "free_windows": [],                          // promotional $0 windows, if any
  "fit_tiers": [{"name": "Solar FiT", "rate": "0.0500", "schedule": []}],
  "conditions": [],                            // eligibility strings, e.g. "Direct debit required"
  "step_tariffs": [],
  "fit_steps": []                              // volume-tiered feed-in only (see rules); else []
}"""

_EXTRACT_PROMPT = """You are an expert at extracting Australian electricity plan pricing data from a web page.

Return ALL electricity plans visible on this page as a JSON ARRAY. Each plan object MUST follow this schema exactly:

""" + _SCHEMA_BLOCK + """

PAGE CONTENT:
""" + _CONTENT_TAG + """

RULES:
- All usage rates are $/kWh and the supply charge is $/day, each as a DECIMAL STRING (e.g. "0.4100"). If the page shows cents (e.g. "41c/kWh" or "41.0¢"), divide by 100 (41c -> "0.4100").
- Day names must be the 3-letter abbreviations: "Mon","Tue","Wed","Thu","Fri","Sat","Sun".
- Times are 24-hour "HH:MM".
- An EMPTY "schedule" array means the tier applies at ALL times (a flat rate). Only fill in a schedule for time-of-use tiers.
- If a plan has no solar feed-in tariff, set "fit_tiers": [].
- If a plan has no free windows or step tariffs, set those to [].
- Enter the EFFECTIVE (already-discounted) rates the customer actually pays, and record any discount/eligibility requirement in "conditions" (there is no separate discount field).
- "fit_steps": ONLY when the feed-in rate depends on how much is exported per day (e.g. "8c on the first 10 kWh/day exported, 4c after"). Then add two fit_tiers and one fit_steps entry: {"threshold_kwh_per_day": 10, "tier_below": "<premium fit name>", "tier_above": "<lower fit name>"}. Otherwise set "fit_steps": [].
- "conditions": capture any eligibility/discount requirements stated for these rates as short strings, e.g. ["Direct debit required", "Pay-on-time discount included"]. Use [] if none are stated.
- Build "plan_id" from the retailer and plan name in snake_case.
- If a value is genuinely not shown, OMIT guessing supply/usage numbers — but still output the plan with whatever rates ARE visible.
- Output ONLY the JSON array. No prose, no markdown fences. If no plans are visible, output [].
"""

_REPAIR_PROMPT = """Some electricity plan JSON objects you (or another model) produced failed validation.

Fix every object below so it satisfies this exact schema, then return ONLY a JSON ARRAY of the corrected plan objects:

""" + _SCHEMA_BLOCK + """

Validation rules: rates are decimal strings in $/kWh (supply in $/day); day names are "Mon".."Sun"; times are 24h "HH:MM"; empty schedule = applies always; conditions is a list of short strings ([] if none); unknown optional fields should be [].

Objects that failed, with their errors:
""" + _FAILURES_TAG + """

Return ONLY a valid JSON array of corrected objects. No markdown, no explanation."""

_CONTEXT_PROMPT = """You are extracting the TARIFF DETAILS of ONE Australian electricity plan from a screenshot or PDF of a retailer's rate sheet.

""" + _CONTEXT_TAG + """

An image of the rates is attached. Extract the rates and return ONE JSON object (not an array) following this schema exactly:

""" + _SCHEMA_BLOCK + """

RULES:
- Usage rates are $/kWh and the supply charge is $/day, as DECIMAL STRINGS. Convert cents if shown (41c/kWh -> "0.4100").
- Day names: "Mon".."Sun". Times: 24h "HH:MM". Empty "schedule" = flat (all times).
- Use the retailer/plan name provided above EXACTLY as given; only fill them from the screenshot if none were provided.
- "conditions": capture any eligibility/discount requirements for these rates (e.g. ["Direct debit required"]). Use [] if none are shown.
- Output ONLY the JSON object. No prose, no markdown fences."""

_IDENTITY_PROMPT = """You are identifying ONE Australian electricity retailer and its plan name from a screenshot or PDF of a rate sheet.

An image of the document is attached.

Look at logos, page headings, footers and any branding to determine:
- "retailer": the electricity company name (e.g. "Amber", "Origin Energy", "Red Energy"). This is REQUIRED.
- "plan_name": the specific plan's display name (e.g. "Smart Plan", "Basic Saver"). Use "" if you cannot tell.

Return ONLY a compact JSON object with exactly these keys:
{"retailer": "...", "plan_name": "..."}

No prose, no markdown fences, no array."""


class PlanExtractor:
    """Extracts ``ElectricityPlan`` objects from page content using an LLM."""

    def __init__(self, provider: LLMProvider, max_repair_attempts: int = 1) -> None:
        self._provider = provider
        self._max_repair_attempts = max(0, max_repair_attempts)

    # ── Public API ─────────────────────────────────────────────────────────────

    def extract_from_text(self, page_text: str) -> list[ElectricityPlan]:
        """Ask the LLM to extract plans from page text content."""
        prompt = _EXTRACT_PROMPT.replace(_CONTENT_TAG, (page_text or "")[:_MAX_CONTENT_CHARS])
        try:
            response = self._provider.complete(prompt)
        except Exception as exc:
            logger.warning("LLM text completion failed: %s", exc)
            return []
        return self._finalize(response)

    def extract_from_screenshot(
        self, screenshot_bytes: bytes, page_text: str = ""
    ) -> list[ElectricityPlan]:
        """Ask a vision-capable LLM to extract plans from a page screenshot.

        ``page_text`` is also embedded in the prompt so that a text-only model
        that ignores the image still receives real content (the Ollama/OpenAI
        providers fall back to text completion when a model rejects images).
        """
        body = (page_text or "").strip()
        content = body[:_MAX_CONTENT_CHARS] if body else (
            "[No extractable text is available — read the rates from the attached screenshot.]"
        )
        content += "\n\n[A screenshot of the page is attached.]"
        prompt = _EXTRACT_PROMPT.replace(_CONTENT_TAG, content)
        try:
            response = self._provider.complete_with_image(prompt, screenshot_bytes)
        except Exception as exc:
            logger.warning("LLM vision completion failed: %s", exc)
            return []
        return self._finalize(response)

    def extract_from_screenshot_with_context(
        self,
        screenshot_bytes: bytes,
        retailer: str = "",
        plan_name: str = "",
        page_text: str = "",
    ) -> list[ElectricityPlan]:
        """Manual-flow extraction where the user already knows the retailer/plan.

        A tailored prompt tells the model the retailer and plan name are fixed,
        so a smaller model only has to read the rates — not identify the brand.
        The user-supplied values always win (applied post-extraction).

        ``page_text`` (e.g. text extracted from an uploaded PDF) is embedded in
        the prompt when supplied, so text-only models and text-based documents
        work even when the image cannot be read.  Mirrors the behaviour of
        :meth:`extract_from_screenshot`.
        """
        ctx_lines = []
        if retailer.strip():
            ctx_lines.append(f'Retailer name (use EXACTLY): "{retailer.strip()}"')
        else:
            ctx_lines.append("Retailer name: infer from the screenshot.")
        if plan_name.strip():
            ctx_lines.append(f'Plan name (use EXACTLY): "{plan_name.strip()}"')
        else:
            ctx_lines.append("Plan name: infer from the screenshot (or omit if unknown).")

        body = (page_text or "").strip()
        if body:
            ctx_lines.append("")
            ctx_lines.append(
                "TEXT CONTENT EXTRACTED FROM THE DOCUMENT "
                "(prefer these figures when they are legible):"
            )
            ctx_lines.append(body[:_MAX_CONTENT_CHARS])

        prompt = _CONTEXT_PROMPT.replace(_CONTEXT_TAG, "\n".join(ctx_lines))

        try:
            response = self._provider.complete_with_image(prompt, screenshot_bytes)
        except Exception as exc:
            logger.warning("LLM vision completion failed: %s", exc)
            return []

        plans = self._finalize(response)
        return [_apply_context(p, retailer, plan_name) for p in plans]

    def infer_identity_from_screenshot(
        self, screenshot_bytes: bytes, page_text: str = ""
    ) -> tuple[str, str]:
        """Ask the LLM to read just the retailer and plan name from a rate sheet.

        Returns ``(retailer, plan_name)`` — ``retailer`` is empty only when the
        model genuinely could not determine it. Used by the Manual tab to
        pre-fill the identity fields (so the user only has to confirm them)
        before the full extraction runs.

        ``page_text`` (e.g. text lifted from an uploaded PDF) is embedded in the
        prompt when supplied, so text-only models can answer without the image.
        """
        prompt = _IDENTITY_PROMPT
        body = (page_text or "").strip()
        if body:
            prompt += (
                "\n\nTEXT CONTENT EXTRACTED FROM THE DOCUMENT "
                "(use this to identify the retailer and plan):\n"
                + body[:_MAX_CONTENT_CHARS]
            )
        try:
            response = self._provider.complete_with_image(prompt, screenshot_bytes)
        except Exception as exc:
            logger.warning("LLM identity inference failed: %s", exc)
            return "", ""
        return _parse_identity_response(response)

    # ── Parsing + repair ───────────────────────────────────────────────────────

    def _finalize(self, response: str) -> list[ElectricityPlan]:
        """Parse an LLM response into validated plans, with a bounded repair pass."""
        data = _parse_json_array(response)
        if data is None:
            logger.warning("LLM returned no parseable JSON array/object.")
            logger.debug("Raw LLM response was: %s", (response or "")[:500])
            return []

        plans, failures = _validate_entries(data)
        attempts = 0
        while failures and attempts < self._max_repair_attempts:
            attempts += 1
            logger.info(
                "Attempting repair pass %d for %d invalid plan entry/entries.",
                attempts, len(failures),
            )
            repaired = self._attempt_repair(failures)
            if not repaired:
                break
            new_plans, failures = _validate_entries(repaired)
            plans.extend(new_plans)

        for _i, entry, err in failures:  # any remaining failures
            logger.warning("Plan entry rejected after repair: %s", err)

        # De-duplicate by plan_id (a repair pass that echoes an already-valid
        # plan must not double-count it).
        seen: set[str] = set()
        unique: list[ElectricityPlan] = []
        for plan in plans:
            if plan.plan_id in seen:
                continue
            seen.add(plan.plan_id)
            unique.append(plan)

        # Stamp the capture time on every extracted plan.  The model is never
        # asked for this value (it would hallucinate dates), so we always set
        # it here.  Plans loaded from hand-authored JSON via ``load_plan`` do
        # not pass through here, so their file value is preserved.
        captured_at = datetime.now().astimezone().isoformat()
        for plan in unique:
            plan.last_updated = captured_at
        return unique

    def _attempt_repair(self, failures: list[tuple[int, Any, str]]) -> list[Any]:
        """Ask the LLM to fix the failing entries; return raw corrected dicts."""
        rendered = "\n\n".join(
            f"Object #{i}:\n{json.dumps(entry, ensure_ascii=False)}\nERROR: {err}"
            for i, entry, err in failures
        )
        prompt = _REPAIR_PROMPT.replace(_FAILURES_TAG, rendered)
        try:
            response = self._provider.complete(prompt)
        except Exception as exc:
            logger.warning("Repair LLM call failed: %s", exc)
            return []
        repaired = _parse_json_array(response)
        return repaired or []


# ── Module-level helpers ───────────────────────────────────────────────────────


def _validate_entries(entries: list[Any]) -> tuple[list[ElectricityPlan], list[tuple[int, Any, str]]]:
    """Validate a list of raw dicts; return (valid_plans, [(index, entry, error), ...])."""
    plans: list[ElectricityPlan] = []
    failures: list[tuple[int, Any, str]] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            failures.append((i, entry, "not a JSON object"))
            continue
        try:
            plans.append(ElectricityPlan.model_validate(entry))
        except Exception as exc:
            failures.append((i, entry, str(exc)))
    return plans, failures


def _parse_json_array(text: str) -> Optional[list[Any]]:
    """Parse an LLM response into a list of objects.

    Accepts a JSON array ``[...]`` or a single object ``{...}`` (wrapped into a
    one-element list).  Returns ``None`` if no JSON could be extracted.
    """
    raw = _extract_json_from_response(text)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Could not parse extracted JSON: %s", exc)
        return None
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return data
    return None


def _extract_json_from_response(text: str) -> str:
    """Extract the first JSON array or object from a possibly-fenced response.

    Markdown code fences (```json ... ```) are stripped first.  We decide
    whether the response is an array or a single object by whichever opening
    delimiter appears first (so a single object that *contains* nested arrays
    is not mistaken for an array).
    """
    if not text:
        return ""

    # Strip markdown code fences.
    stripped = re.sub(r"```(?:json)?\s*", "", text).strip()
    stripped = re.sub(r"```\s*$", "", stripped).strip()

    arr_start = stripped.find("[")
    obj_start = stripped.find("{")
    is_array = arr_start != -1 and (obj_start == -1 or arr_start < obj_start)

    if is_array:
        end = stripped.rfind("]")
        if end > arr_start:
            return stripped[arr_start : end + 1]
    if obj_start != -1:
        end = stripped.rfind("}")
        if end > obj_start:
            return stripped[obj_start : end + 1]
    return ""


def _apply_context(
    plan: ElectricityPlan, retailer: str, plan_name: str
) -> ElectricityPlan:
    """Force user-supplied retailer / plan_name onto an extracted plan.

    Regenerates ``plan_id`` from the final names so it stays consistent.
    Pydantic v2 models are mutable by default, so in-place assignment is safe.
    """
    retailer = (retailer or "").strip()
    plan_name = (plan_name or "").strip()
    changed = False
    if retailer and plan.retailer != retailer:
        plan.retailer = retailer
        changed = True
    if plan_name and plan.plan_name != plan_name:
        plan.plan_name = plan_name
        changed = True
    if changed:
        plan.plan_id = _snake_case(f"{plan.retailer} {plan.plan_name}".strip())
    return plan


def _parse_identity_response(text: str) -> tuple[str, str]:
    """Parse an identity-inference response into ``(retailer, plan_name)``.

    Tolerates markdown fences and extra prose. Missing keys default to "".
    """
    raw = _extract_json_from_response(text)
    if not raw:
        return "", ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    retailer = str(data.get("retailer", "")).strip()
    plan_name = str(data.get("plan_name", "")).strip()
    return retailer, plan_name


def _snake_case(text: str) -> str:
    """Convert arbitrary text into a compact snake_case identifier."""
    norm = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    norm = re.sub(r"[^0-9a-zA-Z]+", "_", norm).strip("_")
    norm = re.sub(r"_+", "_", norm).lower()
    return norm or "plan"
