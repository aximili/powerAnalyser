# Plan JSON Schema

Each `*.json` file in this directory represents one electricity retail offer.

## Top-level fields

| Field | Type | Required | Description |
|---|---|---|---|
| `plan_id` | string | ✓ | Unique identifier (snake_case) |
| `retailer` | string | ✓ | Retailer display name |
| `plan_name` | string | ✓ | Plan display name |
| `valid_from` | ISO date | | Offer start date (informational) |
| `valid_to` | ISO date | | Offer end date (informational) |
| `daily_supply_charge` | decimal string | ✓ | Fixed cost per day in $/day |
| `usage_tiers` | array | ✓ | At least one tier required |
| `free_windows` | array | | Promotional zero-rate windows |
| `fit_tiers` | array | | Solar feed-in credit tiers |
| `step_tariffs` | array | | Daily consumption thresholds |

## TimeRange

Used inside `usage_tiers[*].schedule`, `free_windows[*].schedule`, `fit_tiers[*].schedule`.

```json
{
  "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
  "start": "07:00",
  "end": "23:00"
}
```

- `days`: subset of `["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]`
- `start`: inclusive, `"HH:MM"` 24-hour
- `end`: exclusive, `"HH:MM"` 24-hour
- **Overnight ranges** (`end <= start`, e.g. `"23:00"` → `"07:00"`) are supported

An **empty `schedule` array** means the tier applies at all times on all days (catch-all / flat rate).

## UsageTier

```json
{
  "name": "Peak",
  "rate": "0.4100",
  "schedule": [...]
}
```

`rate` is in $/kWh. Use decimal strings to preserve precision.

## FreeWindow

```json
{
  "name": "Midday Power Saver",
  "schedule": [{"days": ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"], "start": "11:00", "end": "14:00"}],
  "fair_use_cap_kwh": 2.0,
  "overflow_tier": "Shoulder"
}
```

- `fair_use_cap_kwh`: maximum free kWh per calendar day (`null` = no cap)
- `overflow_tier`: name of a `UsageTier` to bill at once the cap is exceeded

## FiTTier

```json
{
  "name": "Solar FiT",
  "rate": "0.0500",
  "schedule": []
}
```

Empty `schedule` = flat credit at all hours.

## StepTariff

```json
{
  "threshold_kwh_per_day": 10.0,
  "tier_below": "Standard",
  "tier_above": "Premium"
}
```

The interval that crosses the threshold is split: the portion below is billed at `tier_below`, the portion above at `tier_above`.
