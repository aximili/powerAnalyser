# NEM12 Period Selection + Multi-Year Averaging

## Goal
After a NEM12 file is selected, show the **available period** and let the user pick
which **period to analyse** (day/month window, optional year choice). When the file
spans >1 year, matching months are **averaged** so seasonal picks use every available
year. Out-of-range picks prompt the user to clamp.

## Confirmed decisions (from Q&A)
1. **Averaging method = "average kWh, then cost".** Build one synthetic period where
   each calendar day's 48 intervals = mean across years; run the existing
   `CostCalculator` unchanged. (`CostCalculator` / `ComparisonEngine` are NOT modified.)
2. **Uniform averaging incl. "All available"** — any selected range collapses
   recurring years per calendar day. A 2-year file under "All available" becomes one
   representative ~1-year period.
3. **Out-of-range = offer to clamp (Yes/No)**; no overlap at all = hard error.
4. **Reporting = period total + $/day** (status quo; no annualization). `period_days`
   becomes the count of distinct calendar days in the resolved (averaged) set.
5. **Parse at selection** — parse NEM12 in a background thread as soon as a file is
   picked/dropped; reuse the parsed `MeterDataSet` at Run (no double parse).
6. **From/To = day/month only** (no year) by default. If the window exists in 2+ years,
   show a **popup** to choose: **Both (averaged)** (default) / `<year> only` / …
7. **Wrap-around allowed** — From > To means crossing year-end (e.g. 1/12–28/2 = summer).

## Known limitation (document, do not block)
Because tariffs are weekday-dependent (`ts.strftime("%a")` at calculator.py:121,
elasticity.py:82) and Strategy A collapses a calendar day across years whose weekdays
differ, weekday-dependent ToU / free-window plans are **slightly smoothed** under
multi-year averaging. Flat / step / 7-day-free-window plans are exact. Reference weekday
is taken from the earliest selected year that contains the date. (Future hardening:
switch to cost-level averaging if exactness is required — out of scope here.)

---

## New module — `src/power_analyser/core/ingestion/period.py`
Pure pandas, fully offline-testable. No GUI, no LLM.

```python
@dataclass
class PeriodResolution:
    meter: MeterDataSet        # filtered + averaged, ready for ComparisonEngine
    period_days: int           # distinct (month, day) in the resolved set
    effective_start_md: tuple[int, int]   # (month, day) actually used
    effective_end_md:   tuple[int, int]
    averaged: bool             # True if any (m,d) combined >1 year
    years_used: list[int]
    notes: list[str]           # human-readable (e.g. "averaged 2025 + 2026")

MonthDay = tuple[int, int]  # (month 1-12, day 1-31)

def available_years(meter) -> list[int]: ...
def years_overlapping_window(meter, from_md: MonthDay, to_md: MonthDay) -> list[int]: ...
def target_calendar_dates(from_md: MonthDay, to_md: MonthDay) -> list[MonthDay]:
    # inclusive; supports wrap (from_md > to_md => wrap year-end);
    # yields only valid calendar dates (skips Feb 30 etc.)
def select_period(
    meter, from_md, to_md, years: list[int] | None = None
) -> PeriodResolution: ...
def build_clamp_message(window, available_md_set) -> str | None: ...
def has_overlap(window, available_md_set) -> bool: ...
```

### `select_period` algorithm
1. `years` None ⇒ all years present. Filter `meter.e1` / `meter.b1` to rows whose
   `(month, day) ∈ target_calendar_dates(from_md, to_md)` and (if `years` given) whose
   absolute year ∈ `years`.
2. **Average per `(month, day)`**: normalize each contributing day to a fixed 48-slot
   grid (00:00–23:30 by interval **position**, so DST short days — 46 slots post-dedup —
   are zero-padded to 48); mean the kWh per position across the contributing days. Do the
   same for `b1` (export) so solar credits are averaged too.
3. **Re-stamp**: assign each averaged day a timestamp on the reference year = earliest
   selected year that contains that `(m,d)`, `pd.date_range(midnight, 48×30min)` then
   `tz_localize("Australia/Melbourne", ambiguous="infer", nonexistent="shift_forward")`.
   Preserve original weekday-of-reference-year for ToU correctness within that year.
4. Build a new `MeterDataSet` (e1, b1, original `nmi`, `start_date`/`end_date` = min/max
   averaged date, `warnings` = original + averaging note).
5. `period_days` = `len(set(e1.index.date))`.

### Clamp / overlap (calendar-window level)
- "available (m,d) set" = union of `(month, day)` over the selected years.
- `has_overlap` = intersection of window target set with available set non-empty.
- If empty ⇒ caller shows hard error: `"No data in the selected period. Available:
  {avail_start}–{avail_end}."`
- If window ⊆ available ⇒ no clamp.
- Else partial ⇒ `build_clamp_message` returns e.g.
  `"Part of your selected period has no data (earliest is 20/5). Trim the start to 20/5?"`
  (and symmetric for the end). Caller does `messagebox.askyesno`; on No ⇒ abort.

---

## GUI — `core_view.py` (Analyse tab)
Insert a new **Analysis Period** frame between the NEM12 bar (row 0) and the Plans
frame (shift Plans→2, Load-Shift→3, bottom→4). Contents:

- `Available period:` label → dynamic text `"20/5/2025 – 20/12/2025"` (full dates with
  year, from `meter.start_date/end_date`), or `"—"` until loaded.
- Segmented control / radio: **`All available`** (default) | **`Custom`**.
- `From` and `To` `CTkEntry`, format **dd/mm** (placeholder `"dd/mm"`); disabled while
  `All available` selected.
- Hint label: `"Day/month. When your file spans multiple years, matching months are
  averaged (choose years at run time)."`

### New state on `CoreView`
- `self._meter: MeterDataSet | None`
- `self._period_mode_var` (`"all"` / `"custom"`), `self._from_var`, `self._to_var`
- Run button disabled with text `Loading…` while `_meter` is being parsed.

### File-select flow (`_set_nem12_path` / `_on_nem12_drop`)
After validating the path, start a daemon thread → `IngestionPipeline().load(path)`; on
success `self.after(0, …)`: store `self._meter`, update available-period label, default
From/To entries to `meter.start_date`/`end_date` (dd/mm), re-enable Run, set status
`"Loaded NEM12: NMI … — {n} days, {start}–{end}"`. On error: clear `_meter`, show error,
disable Run. Picking a new file resets `_meter=None`.

### Year-chooser popup — new `_YearChooserDialog(ctk.CTkToplevel)`
Modal radio list built from `years_overlapping_window(...)`: options
`["Both (averaged)", "2025", "2026", …]` (only shown when ≥2 years overlap; default =
Both). Returns the chosen `years` list (None ⇒ all) or None on Cancel. Keep it tiny;
sanity-check via construct→destroy (not in pytest).

### Run flow (`_run_analysis`, main thread, before background)
1. Guard: meter loaded, plans present (existing checks).
2. If mode `all`: `from_md`/`to_md` = full-year window (1/1–31/12), `years=None`.
3. Else parse `From`/`To` (dd/mm; lenient: accept dd/mm/yyyy and ignore the year).
   Parse failure ⇒ error dialog.
4. Compute `years_overlapping_window`. If ≥2 ⇒ show `_YearChooserDialog`; Cancel ⇒ abort.
   (If user typed a year, pre-select it if present — optional.)
5. `available_md_set` for chosen years; `has_overlap`/`build_clamp_message` →
   `messagebox.askyesno` or hard error as above.
6. Start background thread → `select_period(self._meter, from_md, to_md, years)` then
   `ComparisonEngine().compare(resolution.meter, plans, configs)`. Forward result;
   status includes `resolution.notes`.

`ComparisonResult.period_days` already derives from `meter.e1.index.date` (report.py:125)
so the averaged representative count flows through unchanged.

---

## Settings — `gui/settings.py`
Add to `DEFAULTS`: `"period_mode": "all"`, `"period_from": ""`, `"period_to": ""`.
`CoreView.collect_state` / `_apply_settings` read/write them (dates are loose strings;
ignored if they don't parse).

## CLI parity — `comparison/report.py` (low priority, optional)
Add optional `--from`/`--to` (dd/mm) + `--year {all|YYYY}` to `cli_main`, applying
`select_period` before `engine.compare`. Keeps the headless path feature-complete.

---

## Test plan (all offline; mock-free, pure pandas)

**New `tests/core/test_period_selection.py`** (uses synthetic 2-year `MeterDataSet`s
built like `tests/core/test_calculator.py:_make_meter`):
1. `available_years` + `years_overlapping_window` (incl. wrap window 12/1–2/28).
2. `target_calendar_dates`: normal (6/1–8/31), wrap (12/1–2/28), and that invalid dates
   (2/30) are skipped; counts correct.
3. `select_period` single-year window: only that year's days kept; kWh == input
   (averaging identity on single occurrence); `period_days` correct.
4. `select_period` multi-year **Both**: each `(m,d)` kWh == mean of the two years;
   `period_days` == window length; on a **flat-rate** plan, `total_net` == mean of the
   two per-year totals (exact cross-check since flat plans are weekday-agnostic).
5. `select_period` chosen single year: only that year's data used.
6. Wrap-around window averaging (summer) over a 2-year file.
7. Clamp/overlap: no-overlap ⇒ `has_overlap False`; partial ⇒ `build_clamp_message`
   returns the trim text and exact example wording; fully-inside ⇒ `None`.
8. Idempotency: single-year file, mode all ⇒ identity (same kWh, same day count).
9. DST short day (46-slot) averaged ⇒ output day has 48 slots (zero-padded).
10. `b1` (export) averaged in parallel; solar credit reflects averaged export.

**Extend `tests/test_settings.py`** (headless): assert the three new keys are in
`DEFAULTS` and round-trip through save/load.

**GUI**: no pytest cases (no display). Validation = `python -m py_compile` on touched
files + construct/destroy of `PowerAnalyserApp` and `_YearChooserDialog` per AGENTS.md.

**Regression**: run `.venv\Scripts\python.exe -m pytest tests/ -v` — all existing core
tests must stay green (calculator/ingestion/comparison untouched).

---

## Ordered task list
1. Add `core/ingestion/period.py` with the dataclasses + functions above.
2. Write `tests/core/test_period_selection.py`; iterate until green.
3. Extend `gui/settings.py` `DEFAULTS`; add round-trip test.
4. Add the **Analysis Period** frame + state to `core_view.py`; wire file-select parse
   (background thread) → available-period label + From/To defaults; disable Run while
   loading.
5. Add `_YearChooserDialog`; wire it + clamp/validation into `_run_analysis` (main
   thread) and `select_period` into the background compare.
6. (Optional) `--from/--to/--year` CLI flags in `report.py`.
7. Update `README.md` + `AGENTS.md` (Analyse tab now has period selection + averaging;
   note the weekday-smoothing limitation).
8. Full `pytest tests/ -v` + `py_compile` + construct/destroy sanity check.

## Out of scope
- Cost-level (Strategy B) averaging; weekday-exact multi-year averaging.
- Forced annualization / 365-day projection.
- Persistent caching of parsed NEM12 across restarts (re-parses on each file select).
