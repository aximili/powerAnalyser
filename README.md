# Power Analyser

Victorian residential electricity plan comparison tool.

Upload your smart-meter (NEM12) data, add one or more retailer plan files, and get a ranked cost table with solar feed-in credits and optional load-shift simulation — all calculated locally with no cloud calls required.

An optional AI agent (Part 2) can autonomously browse retailer websites and extract plan data to feed directly into the comparison engine.

---

## Features

- **NEM12 parsing** — handles variable-length DST days (46 or 50 intervals), E1 consumption and B1 solar export
- **Tariff types** — flat rate, time-of-use, 3-part "smart rate", free midday windows (with fair-use caps), solar feed-in, step tariffs
- **Load-shift simulation** — move EV charging / pool pump into free or cheap windows; see how much you save
- **Ranked comparison** — plans sorted by net annual cost with per-plan supply / usage / solar breakdown
- **AI browser agent** — navigates retailer websites, fills suburb/usage forms, extracts plan pricing (supports Ollama, GLM/Zhipu AI, OpenAI-compatible APIs); with screenshot fallback + a self-repair extraction pass for smaller local models
- **Manual rate extract** — paste a screenshot or upload a PDF of a retailer's rate page (with provider/plan name) and extract the plan straight into the comparison
- **Desktop GUI** — CustomTkinter cross-platform app (Windows + Mac) with embedded matplotlib charts

---

## Requirements

- Python 3.9 or newer
- For the GUI: `customtkinter`, `matplotlib`
- For the agent: Playwright browsers installed (`playwright install chromium`)
- For an LLM: Ollama running locally, a GLM API key, or an OpenAI-compatible endpoint

---

## Installation

```bash
# 1. Clone / download the project
cd powerAnalyser

# 2. Create and activate a virtual environment (recommended)
python -m venv .venv
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate

# 3. Install dependencies (third-party packages)
pip install -r requirements.txt

# 4. Install the package itself (editable, so `python -m power_analyser...` works)
pip install -e .

# 5. Install Playwright browser (only needed for the agent)
playwright install chromium

# 6. Copy and edit the environment template
cp .env.example .env   # Windows: copy .env.example .env
# Edit .env with your LLM provider settings
```

> Make sure the virtual environment is activated whenever you run the app or
> the tests. If `python` points at an unrelated interpreter (e.g. another
> project's venv), the `import power_analyser` step will fail.

---

## Usage

### Desktop GUI

```bash
python -m power_analyser.gui.app
```

Three tabs:

| Tab | Purpose |
|---|---|
| **Analyse** | Load NEM12 file, add plan JSONs, configure load-shift, run comparison |
| **Agent** | Set LLM provider and target URL; agent browses and extracts plans automatically |
| **Manual** | Paste/upload a rate-page screenshot or PDF (+ provider/plan name) and extract a plan without the browser |
| **Results** | Ranked table, cost breakdown bar chart, load-shift delta report, CSV export |

### CLI smoke-test (no GUI)

```bash
python -m power_analyser.core.comparison.report \
  --nem12 data/sample_nem12.csv \
  --plans-dir data/plans/
```

---

## Plan JSON format

Plans live in `data/plans/` as JSON files. **See [`docs/plan-schema.md`](docs/plan-schema.md) for the full field reference, the `schedule` semantics, and worked examples for every tariff type** (flat, time-of-use, smart rate, step tariff, free windows, time-varying FiT).

Minimum valid flat-rate plan:

```json
{
  "plan_id": "my_retailer_flat",
  "retailer": "My Retailer",
  "plan_name": "Basic Flat",
  "daily_supply_charge": "0.9800",
  "usage_tiers": [
    { "name": "Flat", "rate": "0.2800", "schedule": [] }
  ],
  "free_windows": [],
  "fit_tiers": [],
  "step_tariffs": []
}
```

Key rules (full detail in [`docs/plan-schema.md`](docs/plan-schema.md)):
- All rates are **decimal strings** in `$/kWh` (supply charge in `$/day`)
- An **empty `schedule`** means the tier applies at all times (a flat/catch-all rate)
- Day names: `Mon Tue Wed Thu Fri Sat Sun`; times in 24-hour `HH:MM`; overnight windows wrap midnight when `end <= start`
- `overflow_tier` / `tier_below` / `tier_above` must match an existing `usage_tiers[].name`
- Optional informational fields: `valid_from`/`valid_to`, `last_updated` (ISO-8601 capture time — auto-stamped by the agent), and `conditions` (e.g. `["Direct debit required"]`)

---

## Running tests

```bash
python -m pytest tests/ -v
```

The test suite (68 tests) runs fully offline — no API keys, no real browser.

```
tests/core/     — NEM12 parsing, ingestion, tariff schema, cost calculator, elasticity
tests/agent/    — plan extractor + orchestrator loop (mock/scripted LLM provider, no browser)
```

---

## Project layout

```
src/power_analyser/
  config.py                  # env-var settings singleton
  core/
    nem12/                   # NEM12 parser (models + parser)
    ingestion/pipeline.py    # NEM12 → pandas DataFrame (DST-aware)
    tariff/                  # Pydantic plan schema + JSON loader
    simulation/
      elasticity.py          # Load-shift simulator
      calculator.py          # Cost calculator (per-interval, Decimal arithmetic)
    comparison/report.py     # Ranked comparison + CLI entry point
  agent/
    llm/                     # LLMProvider ABC + Ollama/GLM/OpenAI implementations
    browser/                 # Playwright controller + CAPTCHA detection
    extractors/              # LLM-driven plan extractor
    orchestrator.py          # Observe → reason → act loop
  gui/
    app.py                   # Main window (3 tabs)
    views/                   # core_view, agent_view, results_view
    widgets/chart_widget.py  # Matplotlib embedded in CustomTkinter

data/
  sample_nem12.csv           # 7-day synthetic NEM12 (includes DST spring-forward)
  plans/                     # Sample plan JSONs

docs/
  plan-schema.md             # Full plan JSON field reference + worked examples
```

---

## Environment variables

Copy `.env.example` to `.env` and fill in as needed:

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | `ollama` / `glm` / `openai` |
| `LLM_MODEL` | `llama3.2` | Model name for the chosen provider |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL |
| `GLM_API_KEY` | — | Zhipu AI API key |
| `OPENAI_API_KEY` | — | OpenAI (or compatible) API key |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Override for local OpenAI-compatible servers |
| `MAX_AGENT_ITERATIONS` | `25` | Maximum observe→act cycles per agent run |
| `AGENT_HEADLESS` | `false` | Run browser headlessly (set `true` for CI) |
| `DATA_DIR` | `data` | Default directory for NEM12 files and plans |

> GUI field values (NEM12 path, LLM provider/model/key, target URL, task
> prompt) are cached in a project-root `.gui_settings.json` so they're
> remembered across restarts. LLM fields also fall back to your `.env` on first
> run. Both files are gitignored — don't commit real keys.---

## Victorian tariff notes

- **DST** — Victoria uses AEDT (UTC+11) in summer and AEST (UTC+10) in winter. Spring-forward (October) days have 46 valid intervals; fall-back (April) days have 50 in the raw file (merged to 48).
- **Free windows** — Amber/AusNet "Midday Power Saver" runs 11:00–14:00 with a 2 kWh/day fair-use cap. Overflow is billed at the shoulder rate.
- **Solar FiT** — time-varying rates (higher during midday solar peak) are supported via `fit_tiers` with a schedule.
