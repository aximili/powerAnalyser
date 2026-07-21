# AGENTS.md — AI Agent Documentation

This document covers the AI browser agent component (Part 2) of Power Analyser.

---

## Purpose (what this app is for)

Power Analyser helps a household find the **best-value Victorian (Australia)
electricity plan for their own usage pattern** over a chosen part of the year
(e.g. summer), based on their **previous year's** smart-meter (NEM12) data.
Victorian retail electricity plans vary wildly and are notoriously hard to read
and compare like-for-like; this tool does the apples-to-apples cost simulation
so the user doesn't have to.

Because the ranked output drives a real purchasing decision, **ranking accuracy
is the product**: a misparsed plan or a miscalculated cost can steer a user onto
a more expensive plan — a real financial loss. Correctly modelling the full
diversity of real VIC tariff structures (ToU, stepped/block, free windows,
time-varying + volume-tiered FiT, controlled load, demand charges, conditional
discounts) is therefore a core requirement, not a nice-to-have.

---

## Workflow for AI agents (read first)

When changing any code in this repository:

1. **Run the tests after every edit** (the full suite is offline — mocked LLM,
   no browser, no network):
   ```bash
   python -m pytest tests/ -v
   ```
   Windows venv: `.venv\Scripts\python.exe -m pytest tests/ -v`
2. **Update or add tests** for any behavioural change. Agent tests live in
   `tests/agent/` and use `MockLLMProvider` / `ScriptedLLMProvider` (no real
   LLM). The orchestrator loop is testable without Playwright via its
   `browser_factory` parameter (see `tests/agent/test_orchestrator.py`).
   Keep every test offline.
3. **Keep docs in sync** — update `README.md` and this file when user-facing
   behaviour, tabs, prompts, schemas, or config keys change.
4. **Never commit secrets.** `.env` and `.gui_settings.json` are gitignored;
   don't put real API keys into code, tests, or docs.
5. For GUI changes, sanity-check with `python -m py_compile` and, where
   possible, construct the window then destroy it immediately
   (`app = PowerAnalyserApp(); app.update(); app.destroy()`) to catch wiring
   errors.
6. **Reflect on accuracy after accuracy-sensitive work** — see the next
   section. Skip for trivial changes (typos, comments, pure formatting, GUI
   wiring that doesn't touch numbers).

---

## Post-task accuracy review (only after accuracy-sensitive work)

Accuracy is critical in this app: a miscalculation or a misparsed plan means
real financial loss for the user. After finishing any change that touches
**numbers or data flow** — i.e. anything in `core/` (tariffs, ingestion,
`period.py`, cost calculator, load-shift elasticity) or `agent/extractors/`
(plan JSON, repair pass) — the agent must do a short reflection pass and, when
there is something genuinely worth doing, surface it. **Stay silent if there is
nothing meaningful to add** — never pad the response with a generic "consider
adding tests".

### Trigger condition

Reflect **only** when the change affects at least one of:
- Tariff math, rounding, or unit conversions
- NEM12 ingestion, DST handling, or the 48-slot normalisation in `period.py`
- Plan extraction JSON, `plan_id` dedup, or `last_updated` stamping
- Multi-year averaging, weekday smoothing, or `ComparisonResult.period_days`
- Eligibility / `conditions` strings that gate a discount

### Sharp edges to check against

- **DST**: 46-row spring-forward days must preserve their 46 real Melbourne
  timestamps — no zero-padding to 48. The averaging loop uses the earliest
  contributing year's actual tz-aware index as the canonical grid and aligns
  other years by wall-clock time (`period.py:_average`).
- **`plan_id`**: dedup in the repair pass must be stable — re-extraction
  overwrites the same file (`core/tariff/loader.py`).
- **`last_updated`**: never taken from the model; stamped at capture time by
  `PlanExtractor._finalize`. Hand-authored `load_plan` JSON preserves the file
  value.
- **Rounding**: flat/step/7-day-free-window plans are exact under multi-year
  averaging; weekday-specific ToU is smoothed (known limitation).
- **`conditions`**: discount-gating strings must flow from the page into the
  schema during extraction.
- **Weekday derivation**: always `_WEEKDAY_NAMES[ts.weekday()]` (locale-
  independent), never `strftime("%a")` — the latter silently breaks schedule
  matching on non-English locales. Applies to `calculator.py` AND
  `elasticity.py`.
- **FiT resolution is order-independent**: a matching scheduled `FiTTier` wins;
  an empty-schedule tier is only a fallback (`_find_active_fit_tier`), exactly
  like usage tiers. A flat catch-all listed first must NOT shadow a
  time-varying rate.
- **Volume-tiered FiT (`fit_steps`)** resolves by cumulative daily *export* and
  is a **separate mode** from time-varying FiT — when `fit_steps` is set, the
  FiT tiers' `schedule` is ignored.
- **Free-window overflow**: once a free window's daily cap is exhausted, ALL
  further in-window usage bills at `overflow_tier` (not the active ToU tier),
  consistent with the cap-crossing interval.
- **Discounts are a convention, not code**: plans carry the *effective*
  (already-discounted) rates; `conditions` documents the requirement. Do not add
  percentage-discount math without an explicit product decision. Controlled
  load, demand charges, seasonal rates, per-quarter blocks, one-off credits and
  wholesale/spot plans are out of scope (see `docs/plan-schema.md`).

### Output format

After the normal final summary, add **one short block** (3–6 lines) headed
**Suggested next step for accuracy** — and only when there is something real and really worth doing.
Format:

- **What** — the specific gap or hardening, with a `file:line` reference.
- **Why it matters** — which sharp edge above it addresses (or a new one), and importance (e.g. critical / high / medium / nice to have / better to omit than cluttering the codebase)
- **Action** — one of:
   - *Do it now* (small, in-context): a one-line command or edit.
   - *New session* (large refactor, deep investigation, or parallel work):
     emit a **self-contained paste-ready prompt** the user can drop into a
     fresh session. It must include: file paths, line refs, the invariant that
     must be preserved, the test command (`python -m pytest tests/ -v`), and a
     note that the existing offline test suite must stay green.

A reflection block that says "looks fine, nothing to add" is worse than no
block — omit it.

### End-of-turn next steps

After finishing a task, when there are **genuine, in-context** follow-ups
the user is likely to want (a small edit, an obvious test to add, a config
tweak, running the suite), surface them as a short numbered list and ask
whether to proceed — so a single `yes` is enough to continue. This mirrors
the ClaudeCode pattern the user is used to:

1. <one-line suggestion>
2. <one-line suggestion>

Want me to do these? Reply `yes` for all, or pick by number.

Rules:

- **Only when there's something real.** No "consider adding tests" /
  "review the code" filler — an empty ending beats a padded one.
- **In-context only.** Large work (a refactor, deep investigation, parallel
  tracks) gets a one-line mention and a pointer to a new session, not an
  inline offer.
- **Accuracy "do it now" items** should also appear in this list (as
  one-liners — the detail stays in the **Suggested next step for accuracy**
  block above), so they get the same `yes` confirmation gate. "New session"
  items stay in the block above with their paste-ready prompt; a one-line
  pointer here is fine but they aren't gated by `yes`.
- Keep the list to **2–4 items**; merge trivial ones.
- This deliberately overrides Kilo's global "don't end with a question"
  default **for this project** — the explicit confirmation gate is the point.

---

## Overview

The agent autonomously navigates electricity retailer websites, fills in household profile forms (state, postcode, usage), and extracts plan pricing into the `ElectricityPlan` JSON schema used by the comparison engine.

It uses a **observe → reason → act** loop driven by a configurable LLM:

```
┌─────────────┐      page text / screenshot       ┌──────────────────┐
│  Browser    │ ────────────────────────────────► │  LLM Provider    │
│  Controller │                                   │  (Ollama / GLM / │
│  (Playwright│ ◄──────────── JSON action ─────── │   OpenAI-compat) │
└─────────────┘                                   └──────────────────┘
       │
       │ extract action
       ▼
  PlanExtractor  ──►  list[ElectricityPlan]  ──►  ComparisonEngine
```

---

## LLM Providers

| Provider | Class | Config key | Vision support |
|---|---|---|---|
| Ollama (local) | `OllamaProvider` | `LLM_PROVIDER=ollama` | Yes (multimodal models) |
| GLM / Zhipu AI | `GLMProvider` | `LLM_PROVIDER=glm` | Yes (GLM-4V auto-selected) |
| OpenAI-compatible | `OpenAIProvider` | `LLM_PROVIDER=openai` | Yes (GPT-4o, etc.) |

All providers implement the `LLMProvider` ABC:

```python
class LLMProvider(ABC):
    def complete(self, prompt: str) -> str: ...
    def complete_with_image(self, prompt: str, image_bytes: bytes) -> str: ...
```

The factory `create_provider(config)` in `agent/llm/base.py` dispatches on `config.llm_provider`.

---

## The Observe → Reason → Act Loop

`AgentOrchestrator.run(task, url, on_captcha, on_plan_found, on_log)` drives the loop:

1. **Observe** — capture page text (up to 12 k characters) and optionally a screenshot
2. **Reason** — send prompt to LLM containing task description, action history (last 5), and page content
3. **Act** — execute the JSON action the LLM returns:

| Action | Effect |
|---|---|
| `navigate` | Go to a different URL |
| `click` | Click a CSS selector |
| `fill` | Type text into a form field |
| `scroll` | Scroll the page by `y` pixels |
| `wait` | Pause 2 seconds |
| `extract` | Run `PlanExtractor` on the current page |
| `done` | Stop; return all accumulated plans |

The loop stops when the LLM returns `done`, when extraction is complete, or when `MAX_AGENT_ITERATIONS` is reached.

### Extraction resilience

The original agent would run `extract` once, get 0 plans, and immediately
declare `done`. The loop now defends against this:

- Every extraction result (including **0 plans**) is written into the action
  history, so the LLM knows whether it actually succeeded.
- When text extraction yields nothing, the orchestrator **automatically
  retries with a page screenshot** (`BrowserController.get_screenshot()`).
  The screenshot prompt also embeds the page text, so a text-only model (e.g.
  a local Gemma/Llama without vision) still has content to work with.
- The agent is **not allowed to stop with `done` while zero plans have been
  found**, until up to `_MAX_FORCED_EXTRACTS` (3) forced screenshot extractions
  have been attempted.
- The Stop button calls `orchestrator.request_stop()`, which halts the loop
  cleanly after the current action.
- `run()` accepts an optional `browser_factory` so the loop can be tested
  without Playwright (see `tests/agent/test_orchestrator.py`).

---

## CAPTCHA Handling

When Playwright navigates to or clicks on a page that triggers a CAPTCHA, `BrowserController` raises `CaptchaDetected`.

The orchestrator:
1. Calls the `on_captcha()` callback (shows a banner in the GUI)
2. Blocks on `threading.Event.wait()` — the agent thread is fully paused
3. Resumes when `orchestrator.signal_captcha_solved()` is called (triggered by the GUI's "Resume" button)

Detection heuristics in `agent/browser/captcha.py`:
- iframe `src` patterns: `recaptcha`, `hcaptcha`, `challenges.cloudflare.com`, `turnstile`
- CSS selectors: `.g-recaptcha`, `.h-captcha`, `#cf-challenge-running`, `[data-hcaptcha-widget-id]`
- Body text substrings: "prove you are human", "verify you are not a robot", etc.

If you encounter a site whose CAPTCHA is not detected, add the relevant selector or text hint to `_CAPTCHA_SELECTORS` / `_CAPTCHA_TEXT_HINTS` in `captcha.py`.

---

## Plan Extractor

`PlanExtractor` in `agent/extractors/plan_extractor.py` asks the LLM to parse page content into `ElectricityPlan` JSON:

- Sends page text (or screenshot for JS-heavy pages) with a structured prompt embedding the exact schema
- Accepts either a JSON array **or** a single `{...}` object (smaller models often omit the array brackets)
- Strips markdown fences from the response
- Runs a bounded **repair pass**: entries that fail pydantic validation are sent back to the LLM together with the validation errors, and the corrected output is merged (de-duplicated by `plan_id`). Set `max_repair_attempts` on the constructor (default 1) to tune/disable this.
- For screenshots, the page text is embedded in the prompt as well, so text-only models still receive content
- `extract_from_screenshot_with_context(bytes, retailer, plan_name, page_text="")` is the entry point for the **Manual** tab: the retailer/plan name are supplied by the user and forced onto the result (`_apply_context`), so the model only reads the rates. When `page_text` is supplied (e.g. text extracted from an uploaded PDF), it is embedded in the prompt so text-only models and text-based documents work without OCR.
- `last_updated` is **never** asked of the model — `PlanExtractor._finalize` stamps it with the capture time (`datetime.now().astimezone().isoformat()`) on every extracted plan. Hand-authored JSON loaded via `load_plan` bypasses this, so the file value is preserved.
- `conditions` (eligibility/discount strings, e.g. "Direct debit required") **is** part of the extraction schema block and prompt, so the model populates it from the page when present.
- Returns `list[ElectricityPlan]`

The extractor is independent of the orchestrator and can be called directly:

```python
from power_analyser.agent.extractors.plan_extractor import PlanExtractor
from power_analyser.agent.llm.ollama_provider import OllamaProvider

provider = OllamaProvider(base_url="http://localhost:11434", model="llama3.2")
extractor = PlanExtractor(provider)
plans = extractor.extract_from_text(page_html)
```

---

## Configuration

Set via `.env` or environment variables:

```
LLM_PROVIDER=ollama          # ollama | glm | openai
LLM_MODEL=llama3.2           # model name
OLLAMA_BASE_URL=http://localhost:11434
GLM_API_KEY=your_key
OPENAI_API_KEY=your_key
OPENAI_BASE_URL=https://api.openai.com/v1   # override for local servers
MAX_AGENT_ITERATIONS=25
AGENT_HEADLESS=false         # true = no visible browser window
```

---

## Running the Agent (GUI)

1. Launch the app: `python -m power_analyser.gui.app`
2. Go to the **Agent** tab
3. Select provider, enter model name and API key / base URL
4. Enter the retailer URL and a task description
5. Click **Start Agent**
6. Watch the live log; solve any CAPTCHAs manually and click **Resume**
7. Extracted plans appear in the **Analyse** tab automatically

### Manual rate extraction (no browser)

When you already have the rates on screen (or in a PDF), skip the autonomous
browser and extract a plan straight from a screenshot or PDF:

1. Open the **Manual** tab (LLM config is shared with the Agent tab)
2. Enter the **provider (retailer) name** and optionally the **plan name**,
   or leave them blank to have the model pre-fill them (see below)
3. **Paste** a screenshot of the rates (`Ctrl+V` / `Cmd+V`), click **Browse…**,
   or drag & drop an image **or PDF** file
4. Click **Extract Plan**

`PlanExtractor.extract_from_screenshot_with_context()` is used: because the
retailer (and optionally plan name) are supplied, smaller local models only
need to read the rates — which is far more reliable than identifying the brand
from the image. Extracted plans flow into the **Analyse** tab automatically.

**Two-phase identity pre-fill.** If the provider name is empty when you click
**Extract Plan**, the app runs a lightweight identity-inference call
(`PlanExtractor.infer_identity_from_screenshot`) that reads just the retailer
and plan name off the rate page, pre-fills the two fields, and asks you to
confirm. The full rate extraction runs on the *second* click, once the provider
name is populated. This keeps small models honest: they confirm identity first,
then read the rates.

**Persistence.** Each extracted plan is upserted to
`data/plans/{plan_id}.json` via `save_plan` in
`core/tariff/loader.py` (keyed on `plan_id`, so re-extracting a plan updates
the same file). The saved path is reported in the result box. This is the same
directory the **Analyse** tab and the comparison engine load plans from, so an
extracted plan sticks around across restarts and is picked up by "Add Folder…".

**PDFs** (`.pdf`) are rendered with PyMuPDF (`agent/extractors/pdf_utils.py`):
the pages are stitched into one image (fed to a vision model) and their text is
extracted and embedded in the prompt (so text-only models and selectable-text
rate sheets work too). PDFs are accepted via **Browse…** and drag & drop (not
clipboard paste). If PyMuPDF is missing, a clear error is shown. The preview
panel is height-bounded so a large multi-page PDF never hides the **Extract
Plan** button.

---

## Running Tests

The suite runs fully offline — no API keys, no real browser:

```bash
python -m pytest tests/ -v
```

- `tests/agent/` — plan extraction (`MockLLMProvider`) and the orchestrator
  loop (`ScriptedLLMProvider` + a fake browser via `browser_factory`).
- `tests/core/` — NEM12 parsing, ingestion, tariff schema, cost calculator,
  load-shift elasticity, **period selection + multi-year averaging**
  (`test_period_selection.py`), **comparison engine** (`test_report.py`), and
  **JSON loader round-trip + upsert + malformed-JSON handling**
  (`test_loader.py`). The cost-calculator suite includes a passing test for
  the per-window-step-threshold fix
  (`test_free_window_consumption_should_not_consume_step_threshold`) and a
  characterisation test for `step_tariffs[1]` being silently ignored
  (`test_second_step_tariff_is_silently_ignored`).
- `tests/test_settings.py` — GUI settings persistence (incl. the
  `period_mode`/`period_from`/`period_to` keys).

When you add or change behaviour, add a test alongside it (see the workflow
section at the top of this file).

---

## Analysis Period selection + multi-year averaging

On the **Analyse** tab, picking a NEM12 file parses it **in the background**
(`CoreView._begin_parse` → daemon thread → `IngestionPipeline().load()`). The
resulting `MeterDataSet` is cached on `CoreView._meter` and **reused at Run**
— no double-parse. The **Available period** label shows
`start_date–end_date` (full `dd/mm/yyyy`); the Run button is disabled (text
`Loading…`) until the parse completes.

### Window & year selection

- **All available** (default) → full-year window `(1/1)–(12/31)`, `years=None`.
- **Custom** → `From`/`To` entries in `dd/mm` (a `dd/mm/yyyy` value is accepted
  but the year is ignored). Wrap-around is allowed: `From > To` crosses
  year-end (e.g. `1/12–28/2` = summer).
- When a custom window exists in **≥2 years**, `_YearChooserDialog` (a modal
  `CTkToplevel`) offers **Both (averaged)** (default → `years=None`) or a single
  year. Cancel aborts the run.

### Clamp / overlap (calendar-window level)

Computed before the background compare, on the main thread:

- `available_month_days(meter, years)` = union of `(month, day)` over the
  selected years.
- `has_overlap(window, avail)` `False` → hard error dialog.
- `build_clamp_message(window, avail)` returns a trim prompt for a partial
  window → `messagebox.askyesno`; **No** aborts.

### `select_period` (`core/ingestion/period.py`)

Pure pandas, offline-testable. Returns a `PeriodResolution(meter, period_days,
effective_start_md, effective_end_md, averaged, years_used, notes)`. The
resolution's `meter` is fed unchanged into `ComparisonEngine.compare`;
`ComparisonResult.period_days` derives from `meter.e1.index.date`
(`report.py:125`) so the averaged representative day count flows through.

Algorithm:

1. Filter `e1`/`b1` rows whose `(month, day)` ∈ `target_calendar_dates(...)`
   and (if `years` given) whose year ∈ `years`.
2. **Average per `(month, day)`** by **wall-clock time alignment**: for each
   `(month, day)` group, the earliest contributing year's real tz-aware
   timestamps become the canonical index (so spring-forward days keep 46 slots,
   normal and fall-back days keep 48). Each other year's data is aligned to
   that grid by `time()` lookup; slots absent in a given year (e.g. 02:00/02:30
   from a year where that date isn't spring-forward) contribute `NaN` and are
   excluded from the `nanmean`. `b1` is averaged the same way so solar credits
   are averaged too.
3. The output index **is** the canonical index from step 2 — no synthetic
   re-stamp via `shift_forward`. The reference year's timestamps are used
   verbatim.
4. Build a new `MeterDataSet`; `period_days = len(set(e1.index.date))`.

### Known limitation (weekday smoothing)

Averaging is **kWh-level** ("average then cost"). Because weekdays differ
across years for the same calendar date, weekday-specific ToU / free-window
plans are **slightly smoothed** under multi-year averaging. Flat, step, and
7-day-free-window plans are exact (verified in
`test_select_period_flat_rate_averaged_equals_mean_of_years`). Cost-level
(Strategy B) averaging is out of scope.

When `ComparisonEngine.compare()` is called with a `resolution` whose
`years_used` has more than one entry, it checks whether any plan restricts a
usage tier or free window to a subset of the week. If so, it appends a
plain-English warning to `ComparisonResult.warnings` (once, regardless of how
many plans qualify), advising the user to select a single year for best
accuracy.

### Settings

`gui/settings.py` `DEFAULTS` adds `period_mode` (`"all"`/`"custom"`),
`period_from`, `period_to` (loose `dd/mm` strings; ignored if they don't
parse). `CoreView.collect_state` / `_apply_settings` round-trip them.

### CLI parity

`cli_main` accepts optional `--from`/`--to` (`dd/mm`) and
`--year {all|YYYY}`, applying `select_period` before `engine.compare`.

---

## Extending

### Adding a new LLM provider

1. Create `src/power_analyser/agent/llm/my_provider.py`
2. Subclass `LLMProvider` and implement `complete` and `complete_with_image`
3. Add a branch in `create_provider()` in `base.py`
4. Add `my_provider` to the GUI dropdown in `agent_view.py`

### Adding a new CAPTCHA pattern

Edit `_CAPTCHA_SELECTORS` or `_CAPTCHA_TEXT_HINTS` in `agent/browser/captcha.py`.

### Improving extraction accuracy

Edit the `_EXTRACT_PROMPT` in `agent/extractors/plan_extractor.py`.  Adding few-shot examples (real plan JSON) significantly improves extraction accuracy for GPT-4-class models.
