# Plan JSON files

Each `*.json` file in this directory is **one electricity retail offer**. The
loader (`tariff/loader.py`) reads every `*.json` here and validates it against
the `ElectricityPlan` model.

## Schema — single source of truth

This directory does **not** duplicate the schema. To avoid drift, there is one
reference:

- **Authoritative:** the Pydantic models in
  [`src/power_analyser/core/tariff/schema.py`](../../src/power_analyser/core/tariff/schema.py)
  — the code that actually validates every plan.
- **Human-readable reference:** [`docs/plan-schema.md`](../../docs/plan-schema.md)
  — every field, every validation rule, and worked examples for each tariff
  type. Kept in sync with `schema.py`.

Start with `docs/plan-schema.md`; consult `schema.py` when in doubt.

## Sample plans in this directory

| File | Demonstrates |
|---|---|
| `sample_flat_rate.json` | Minimum valid plan (single flat usage tier) |
| `sample_time_of_use.json` | Peak / off-peak time-of-use |
| `sample_smart_rate_free_window.json` | 3-part Smart Rate + capped free window + time-varying FiT |
| `sample_volume_tiered_fit.json` | Volume-tiered solar feed-in (`fit_steps`) |
| `globird_four4free.json`, `globird_zerohero.json` | Real-world reference plans |
