# Plan JSON schema reference

This document is the complete reference for the **plan JSON** files consumed by
Power Analyser. It is the source of truth for the `ElectricityPlan` schema
defined in [`src/power_analyser/core/tariff/schema.py`](../src/power_analyser/core/tariff/schema.py).

> The README keeps a short summary; this file has every field, every rule, and
> worked examples for each tariff type.

---

## Where plan files live

Plans are stored as `*.json` files in a plans directory (default `data/plans/`).
Each file is **one plan**. The loader (`tariff/loader.py`) reads every
`*.json` in the directory and validates it against `ElectricityPlan`.

```python
from power_analyser.core.tariff.loader import load_plan
plan = load_plan(Path("data/plans/sample_flat_rate.json"))
```

---

## Top-level fields

A plan encodes four evaluation layers that the calculation engine processes for
every 30-minute interval:

1. **Fixed overhead** — `daily_supply_charge` (once per calendar day)
2. **Usage tiers** — flat, time-of-use, or 3-part "Smart Rate"
3. **Incentive windows** — `free_windows` (free or discounted periods, optional daily cap)
4. **Solar FiT** — `fit_tiers` (feed-in credits for export)

| Field | Type | Required | Description |
|---|---|---|---|
| `plan_id` | string | yes | Unique snake_case identifier (e.g. `amber_smart_plan`). |
| `retailer` | string | yes | Retailer / provider name (e.g. `Amber Electric`). |
| `plan_name` | string | yes | Plan display name (e.g. `Smart Plan`). |
| `daily_supply_charge` | decimal string | yes | Fixed cost in **$/day**, e.g. `"0.9800"`. |
| `usage_tiers` | array | yes (≥1) | One or more usage rate bands. See [Usage tiers](#usagetier). |
| `free_windows` | array | no (default `[]`) | Promotional $0 / discounted windows. See [Free windows](#freewindow). |
| `fit_tiers` | array | no (default `[]`) | Solar feed-in credit tiers. See [FiT tiers](#fittier). |
| `step_tariffs` | array | no (default `[]`) | Daily **consumption** thresholds. See [Step tariffs](#steptariff). |
| `fit_steps` | array | no (default `[]`) | Daily **export** thresholds (volume-tiered feed-in). See [FiT steps](#fitstep). |
| `valid_from` | string | no | ISO date the rates start (informational), e.g. `"2024-01-01"`. |
| `valid_to` | string | no | ISO date the rates end (informational), e.g. `"2025-12-31"`. |
| `last_updated` | string | no | ISO-8601 **datetime** the data was captured/edited (informational), e.g. `"2026-07-14T11:30:00+10:00"`. See [last_updated & conditions](#last_updated--conditions). |
| `conditions` | array of strings | no (default `[]`) | Eligibility/discount conditions for these rates (informational), e.g. `["Direct debit required"]`. |

### The three informational fields

`valid_from`, `valid_to`, `last_updated`, and `conditions` are **informational
only** — they are stored, surfaced in the results/CSV, but never used by the
cost calculator. They exist so a stale or conditional plan is self-documenting.

---

## Data type conventions

- **All rates are decimal strings**, not numbers, to avoid floating-point drift
  across 17,520 intervals/year. Usage/FiT rates are `$/kWh`; the supply charge
  is `$/day`.
  ```json
  "rate": "0.4100"        // $/kWh
  "daily_supply_charge": "0.9800"   // $/day
  ```
  If a retailer page shows cents (`41c/kWh`), divide by 100 → `"0.4100"`.
- **Day names** are the 3-letter abbreviations: `Mon Tue Wed Thu Fri Sat Sun`.
- **Times** are 24-hour `HH:MM`. `start` is inclusive, `end` is exclusive.
- **Wrap-around windows** (e.g. overnight) are written with `end <= start`.
  A window of `23:00 → 07:00` spans midnight automatically.

---

## The `schedule`

A `schedule` is a list of `{ days, start, end }` time windows. It appears on
usage tiers, free windows, and FiT tiers.

```json
"schedule": [
  { "days": ["Mon","Tue","Wed","Thu","Fri"], "start": "07:00", "end": "23:00" }
]
```

### Empty schedule = "all times"

An **empty `schedule: []`** means the tier/window applies at **all times on all
days**. This is how you write a flat rate, or the catch-all off-peak tier in a
time-of-use plan:

```json
{ "name": "Off-Peak", "rate": "0.1500", "schedule": [] }
```

### Multiple windows in one schedule

A single tier can list several windows. They are OR-ed together. For example, a
"shoulder" tier covering weekday daytimes + evenings, plus all weekend daytime:

```json
{
  "name": "Shoulder",
  "rate": "0.2600",
  "schedule": [
    { "days": ["Mon","Tue","Wed","Thu","Fri"], "start": "07:00", "end": "17:00" },
    { "days": ["Mon","Tue","Wed","Thu","Fri"], "start": "21:00", "end": "23:00" },
    { "days": ["Sat","Sun"], "start": "07:00", "end": "23:00" }
  ]
}
```

### Overnight (wrap-midnight) window

Because `end` is exclusive and ranges wrap when `end <= start`, a nightly
off-peak band like 23:00→07:00 is just:

```json
{ "days": ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"], "start": "23:00", "end": "07:00" }
```

---

## Sub-model reference

### TimeRange

A recurring window on specified days of the week.

| Field | Type | Required | Notes |
|---|---|---|---|
| `days` | array of day names | yes (≥1) | Subset of `Mon..Sun`. |
| `start` | `HH:MM` | yes | Inclusive. Must differ from `end`. |
| `end` | `HH:MM` | yes | Exclusive; `end <= start` wraps past midnight. Must differ from `start`. |

`start` and `end` must not be equal — an equal start/end would match every
time of day and is rejected as a likely misconfiguration. Use an empty
`schedule: []` for a genuine catch-all/all-times tier instead.

### UsageTier

One rate band within a usage structure.

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Referenced by free windows & step tariffs. |
| `rate` | decimal string | yes | `$/kWh`, `≥ 0`. |
| `schedule` | array of TimeRange | no (default `[]`) | Empty = applies at all times (catch-all). |

At each interval the engine picks **one** usage tier: the first whose schedule
matches the current day/time, falling back to the empty-schedule (catch-all)
tier. Every plan must therefore have at least one tier that is either flat
(empty schedule) or whose schedules collectively leave a catch-all.

### FreeWindow

A zero-cost promotional window with an optional daily cap.

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | — |
| `schedule` | array of TimeRange | yes (≥1) | When the free period runs. |
| `fair_use_cap_kwh` | number or null | no (default null) | Max free kWh/day. `null` = uncapped. |
| `overflow_tier` | string | yes | `UsageTier.name` billed **after** the cap is hit. |

### FiTTier

A solar feed-in credit rate for export.

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | — |
| `rate` | decimal string | yes | Credit `$/kWh`, `≥ 0`. |
| `schedule` | array of TimeRange | no (default `[]`) | Empty = flat FiT (all times). |

### StepTariff

A daily consumption threshold that triggers a higher rate. The interval that
crosses `threshold_kwh_per_day` is split: the portion below the threshold bills
at `tier_below`, the portion above at `tier_above`.

| Field | Type | Required | Notes |
|---|---|---|---|
| `threshold_kwh_per_day` | number | yes | `> 0`. |
| `tier_below` | string | yes | `UsageTier.name` below the threshold. |
| `tier_above` | string | yes | `UsageTier.name` above the threshold. |

### FiTStep

A daily **export** threshold that changes the feed-in credit rate — the FiT
twin of `StepTariff`. Once cumulative daily *export* crosses
`threshold_kwh_per_day`, the crossing interval splits: export below the
threshold is credited at `tier_below`, above at `tier_above` (both naming a
`FiTTier`). Models "premium feed-in on the first N kWh/day exported, lower rate
after" (e.g. 8c on the first 10 kWh/day, 4c beyond).

| Field | Type | Required | Notes |
|---|---|---|---|
| `threshold_kwh_per_day` | number | yes | `> 0`. Cumulative **export** kWh/day. |
| `tier_below` | string | yes | `FiTTier.name` credited below the threshold. |
| `tier_above` | string | yes | `FiTTier.name` credited above the threshold. |

> **Volume-tiered and time-varying FiT are separate modes.** When `fit_steps`
> is present the engine resolves FiT purely by export volume and the referenced
> tiers' `schedule` is ignored. Use time-varying FiT (`FiTTier.schedule`) *or*
> volume-tiered FiT (`fit_steps`), not both on one plan.

---

## Validation rules

- `usage_tiers` must contain **at least one** entry.
- All rates and the supply charge must be `≥ 0`; `threshold_kwh_per_day > 0`.
- `TimeRange.days` must be **non-empty**.
- `TimeRange.start` and `TimeRange.end` must **differ** (an equal start/end
  would match all times — use an empty `schedule` for a catch-all instead).
- `FreeWindow.overflow_tier` is **required** whenever `fair_use_cap_kwh` is set.
- Every **name reference** must point to an existing tier:
  - `FreeWindow.overflow_tier` → `usage_tiers[].name`
  - `StepTariff.tier_below` / `StepTariff.tier_above` → `usage_tiers[].name`
  - `FiTStep.tier_below` / `FiTStep.tier_above` → `fit_tiers[].name`
  - A dangling reference raises a `ValueError` at load time.
- `StepTariff` and `FiTStep` `tier_below` and `tier_above` must differ.

---

## Worked examples

### 1. Flat rate (minimum valid plan)

```json
{
  "plan_id": "sample_flat_rate",
  "retailer": "Sample Retailer A",
  "plan_name": "Simple Saver (Flat Rate)",
  "daily_supply_charge": "0.9200",
  "usage_tiers": [
    { "name": "Flat", "rate": "0.2800", "schedule": [] }
  ],
  "free_windows": [],
  "fit_tiers": [{ "name": "Solar FiT", "rate": "0.0600", "schedule": [] }],
  "step_tariffs": []
}
```

### 2. Time of use (peak / off-peak)

```json
{
  "plan_id": "sample_tou",
  "retailer": "Sample Retailer B",
  "plan_name": "Flex Saver (Time of Use)",
  "daily_supply_charge": "0.9800",
  "usage_tiers": [
    {
      "name": "Peak",
      "rate": "0.4100",
      "schedule": [
        { "days": ["Mon","Tue","Wed","Thu","Fri"], "start": "07:00", "end": "23:00" }
      ]
    },
    { "name": "Off-Peak", "rate": "0.1750", "schedule": [] }
  ],
  "free_windows": [],
  "fit_tiers": [{ "name": "Solar FiT", "rate": "0.0500", "schedule": [] }],
  "step_tariffs": []
}
```

### 3. Smart rate (3-part) + free midday window + time-varying FiT

This is the most complex shape: peak/shoulder/off-peak tiers, a capped free
midday window that overflows to the shoulder rate, and a higher midday solar
feed-in.

```json
{
  "plan_id": "sample_smart_rate",
  "retailer": "Sample Retailer C",
  "plan_name": "Smart Saver (3-Part + Midday Power Saver)",
  "valid_from": "2024-01-01",
  "valid_to": "2025-12-31",
  "last_updated": "2026-07-14T11:30:00+10:00",
  "conditions": ["Direct debit required", "Pay-on-time discount included"],
  "daily_supply_charge": "1.0500",
  "usage_tiers": [
    {
      "name": "Peak",
      "rate": "0.4800",
      "schedule": [
        { "days": ["Mon","Tue","Wed","Thu","Fri"], "start": "17:00", "end": "21:00" }
      ]
    },
    {
      "name": "Shoulder",
      "rate": "0.2600",
      "schedule": [
        { "days": ["Mon","Tue","Wed","Thu","Fri"], "start": "07:00", "end": "17:00" },
        { "days": ["Mon","Tue","Wed","Thu","Fri"], "start": "21:00", "end": "23:00" },
        { "days": ["Sat","Sun"], "start": "07:00", "end": "23:00" }
      ]
    },
    { "name": "Off-Peak", "rate": "0.1500", "schedule": [] }
  ],
  "free_windows": [
    {
      "name": "Midday Power Saver",
      "schedule": [
        { "days": ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"], "start": "11:00", "end": "14:00" }
      ],
      "fair_use_cap_kwh": 2.0,
      "overflow_tier": "Shoulder"
    }
  ],
  "fit_tiers": [
    {
      "name": "Peak Solar FiT",
      "rate": "0.1000",
      "schedule": [
        { "days": ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"], "start": "10:00", "end": "15:00" }
      ]
    },
    { "name": "Standard Solar FiT", "rate": "0.0500", "schedule": [] }
  ],
  "step_tariffs": []
}
```

### 4. Step tariff (block pricing)

First 5 kWh/day at the low rate, everything above at the high rate:

```json
{
  "plan_id": "sample_step",
  "retailer": "Sample Retailer D",
  "plan_name": "Block Saver",
  "daily_supply_charge": "1.0000",
  "usage_tiers": [
    { "name": "Low", "rate": "0.2000", "schedule": [] },
    { "name": "High", "rate": "0.4000", "schedule": [] }
  ],
  "free_windows": [],
  "fit_tiers": [],
  "step_tariffs": [
    { "threshold_kwh_per_day": 5.0, "tier_below": "Low", "tier_above": "High" }
  ]
}
```

### 5. Volume-tiered solar feed-in (`fit_steps`)

Premium feed-in on the first 10 kWh exported each day, a lower rate beyond —
common on post-deregulation VIC solar plans (e.g. AGL Solar Savers):

```json
{
  "plan_id": "sample_volume_tiered_fit",
  "retailer": "Sample Retailer E",
  "plan_name": "Solar Saver (Volume-Tiered Feed-in)",
  "daily_supply_charge": "0.9500",
  "usage_tiers": [
    { "name": "Flat", "rate": "0.2800", "schedule": [] }
  ],
  "free_windows": [],
  "fit_tiers": [
    { "name": "Premium export", "rate": "0.0800", "schedule": [] },
    { "name": "Excess export", "rate": "0.0400", "schedule": [] }
  ],
  "step_tariffs": [],
  "fit_steps": [
    { "threshold_kwh_per_day": 10.0, "tier_below": "Premium export", "tier_above": "Excess export" }
  ]
}
```

---

## Discounts & effective rates

The engine does **not** compute conditional or guaranteed percentage discounts
(pay-on-time, direct-debit, etc.). To keep plan modelling simple and robust,
**enter the effective (already-discounted) rates you expect to pay** in
`daily_supply_charge` and the usage/FiT rates, and record what the customer must
do to get them in `conditions` (e.g. `"Pay-on-time discount applied"`,
`"Direct debit required"`).

- A conditional discount (e.g. "34% off usage + supply if you pay on time")
  usually applies to **both** usage and supply — bake it into every rate and the
  supply charge, not just one.
- `conditions` is informational only; it never changes the computed cost. It
  documents the assumption behind the effective rates so the ranking stays
  honest and auditable.

---

## `last_updated` & conditions

These two fields make a plan file self-describing about **when** it was
captured and **what you must do** to get the rates shown.

### `last_updated`

An ISO-8601 datetime string (with timezone offset recommended), e.g.
`"2026-07-14T11:30:00+10:00"`. Stored as a plain string (lenient) and
informational only.

- **Agent / Manual extraction** — the application stamps `last_updated`
  automatically with the capture time. You do **not** ask the LLM for it, and
  you do not need to set it in the JSON the agent produces.
- **Hand-authored files** — set it to the date you wrote/checked the rates.

### `conditions`

A list of short human-readable strings capturing the eligibility or discount
requirements attached to the rates, for example:

```json
"conditions": ["Direct debit required", "Pay-on-time discount included"]
```

- Use `[]` (or omit the field) when there are no special conditions.
- Surfaced in the exported CSV (joined with `"; "`) and otherwise informational.

---

## Not modelled (known limitations)

These real-world structures are intentionally **out of scope**. Where one
applies, fold it into the effective rates above or treat the ranking as an
approximation:

- **Controlled load / dedicated circuits** (off-peak hot water, pool pump on a
  separate meter element). Only the `E1` (general consumption) and `B1` (solar
  export) NEM12 streams are ingested; a controlled-load stream is not read, so
  that consumption and its separate tariff are not costed.
- **Demand charges** ($/kW of peak demand). Not modelled. (Victorian
  *residential* demand tariffs are being retired from 1 July 2026, so this
  mainly affects legacy/business data.)
- **Seasonal rates** — schedules vary by day-of-week and time only, not by
  date/season. (Standard VIC residential usage rates are not seasonal.)
- **Quarterly / billing-period block thresholds** — `step_tariffs` and
  `fit_steps` thresholds are **per day**. (VIC residential blocks are daily;
  per-quarter blocks are a business/other-state structure.)
- **One-off credits** (sign-up / welcome credits) — a period-cost ranking would
  be distorted by a one-off amount; factor these in yourself when weighing plans.
- **Wholesale/spot pass-through plans** (e.g. Amber) — these need an external
  30-minute AEMO price series, which is outside the self-contained plan-JSON
  model.

---

## Extending the schema

When adding a field, update, in order:

1. `ElectricityPlan` (or the relevant sub-model) in
   [`schema.py`](../src/power_analyser/core/tariff/schema.py).
2. The `_SCHEMA_BLOCK` + prompt rules in
   [`plan_extractor.py`](../src/power_analyser/agent/extractors/plan_extractor.py)
   if the agent should extract it.
3. The sample plans in `data/plans/`.
4. The tests in `tests/core/` and `tests/agent/`.
5. This document and the README summary.
