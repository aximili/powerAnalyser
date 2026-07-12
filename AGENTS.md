# AGENTS.md — AI Agent Documentation

This document covers the AI browser agent component (Part 2) of Power Analyser.

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
- Strips markdown fences from the response
- Parses and pydantic-validates each entry; skips (with a warning) any entry that fails
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

---

## Running Tests

The agent tests use `MockLLMProvider` (no real LLM, no network):

```bash
PYTHONPATH=src python -m pytest tests/agent/ -v
```

9 tests cover plan extraction: valid JSON, partial validity, markdown fences, empty responses, image hints, and prompt content verification.

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
