"""NEM12 period selection and multi-year averaging.

Lets the user choose a day/month window (with optional year selection) and
collapses matching calendar days across years into one representative period
by averaging kWh per wall-clock time slot. The resulting :class:`MeterDataSet`
is fed unchanged into ``CostCalculator`` / ``ComparisonEngine`` — neither of
those modules is modified.

Averaging method = "average kWh, then cost" (Strategy A). For each
``(month, day)`` pair the output index is taken from the earliest contributing
year's actual tz-aware timestamps — preserving the real slot count for DST
days (46 for spring-forward, 48 for normal and fall-back). Years are aligned
by wall-clock time (not array position), and slots absent in a given year are
excluded from the mean (NaN) rather than pulling it toward zero.

Known limitation: because weekdays differ across years for the same calendar
date, weekday-specific ToU / free-window plans are slightly smoothed under
multi-year averaging. Flat / step / 7-day-free-window plans are exact. See
``AGENTS.md`` and the plan for details.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .pipeline import MELBOURNE_TZ, MeterDataSet

MonthDay = tuple[int, int]  # (month 1-12, day 1-31)

#: Leap reference year used to enumerate valid calendar ``(month, day)``
#: pairs — leap so Feb 29 is a real date while Feb 30 is not.
_REF_LEAP_YEAR = 2000


@dataclass
class PeriodResolution:
    """Outcome of :func:`select_period`.

    ``meter`` is the filtered + averaged dataset ready for ``ComparisonEngine``.
    The remaining fields are reported to the user / status bar.
    """

    meter: MeterDataSet
    period_days: int
    effective_start_md: MonthDay
    effective_end_md: MonthDay
    averaged: bool
    years_used: list[int]
    notes: list[str] = field(default_factory=list)


# ── Public helpers ────────────────────────────────────────────────────────────


def available_years(meter: MeterDataSet) -> list[int]:
    """Return the sorted set of years present in the meter data."""
    if meter.e1.empty:
        return []
    return sorted({d.year for d in meter.e1.index.date})


def available_month_days(
    meter: MeterDataSet, years: list[int] | None = None
) -> set[MonthDay]:
    """Union of ``(month, day)`` over the selected years (all if ``years`` None)."""
    year_set: set[int] | None = set(years) if years is not None else None
    out: set[MonthDay] = set()
    for d in meter.e1.index.date:
        if year_set is None or d.year in year_set:
            out.add((d.month, d.day))
    return out


def years_overlapping_window(
    meter: MeterDataSet, from_md: MonthDay, to_md: MonthDay
) -> list[int]:
    """Years that contain at least one ``(month, day)`` inside the window."""
    target = set(target_calendar_dates(from_md, to_md))
    out: set[int] = set()
    for d in meter.e1.index.date:
        if (d.month, d.day) in target:
            out.add(d.year)
    return sorted(out)


def target_calendar_dates(from_md: MonthDay, to_md: MonthDay) -> list[MonthDay]:
    """Inclusive list of valid ``(month, day)`` pairs for the window.

    Supports wrap-around: ``from_md > to_md`` crosses year-end
    (e.g. ``(12, 1)`` → ``(2, 28)`` = summer). Invalid intermediate dates
    (Feb 30, Apr 31, …) are skipped because they can never be constructed.
    Raises ``ValueError`` if an endpoint is itself not a real calendar date.
    """
    if from_md <= to_md:
        start = _ref_date(from_md)
        end = _ref_date(to_md)
    else:
        # Wrap-around window crosses year-end (e.g. Nov → Feb).
        # end must be in a year strictly after start's year.
        # Feb 29 end requires a leap year; year 2001 has no Feb 29, so use
        # year 1999 for start and _REF_LEAP_YEAR (2000) for end in that case.
        if to_md == (2, 29):
            start = _ref_date(from_md, year=_REF_LEAP_YEAR - 1)  # 1999
            end = _ref_date(to_md, year=_REF_LEAP_YEAR)           # 2000-02-29
        else:
            start = _ref_date(from_md)                             # 2000
            end = _ref_date(to_md, year=_REF_LEAP_YEAR + 1)       # 2001

    out: list[MonthDay] = []
    d = start
    while d <= end:
        out.append((d.month, d.day))
        d += datetime.timedelta(days=1)
    return out


def has_overlap(
    window: list[MonthDay], available_md_set: set[MonthDay]
) -> bool:
    """True if the window intersects the available ``(month, day)`` set at all."""
    return bool(set(window) & available_md_set)


def build_clamp_message(
    window: list[MonthDay], available_md_set: set[MonthDay]
) -> str | None:
    """Return a trim prompt if the window is only partially covered, else None.

    Returns ``None`` when every date in ``window`` is available (no clamp
    needed) or when nothing overlaps (the caller shows a hard error instead).
    Otherwise returns a single Yes/No prompt offering to trim the start and/or
    end of the window to the first/last available date.
    """
    missing = {md for md in window if md not in available_md_set}
    if not missing:
        return None  # full coverage
    present = [md for md in window if md in available_md_set]
    if not present:
        return None  # no overlap — caller handles the hard error

    first = present[0]
    last = present[-1]
    first_idx = window.index(first)
    last_idx = window.index(last)
    start_missing = any(md in missing for md in window[:first_idx])
    end_missing = any(md in missing for md in window[last_idx + 1:])

    interior_count = sum(1 for md in window[first_idx + 1:last_idx] if md in missing)
    gap_note = (
        f" Also, {interior_count} day(s) missing within the selected period."
        if interior_count else ""
    )

    if start_missing and end_missing:
        return (
            f"Part of your selected period has no data "
            f"(available {_fmt(first)}–{_fmt(last)}). "
            f"Trim to {_fmt(first)}–{_fmt(last)}?"
            + gap_note
        )
    if start_missing:
        return (
            f"Part of your selected period has no data "
            f"(earliest available is {_fmt(first)}). "
            f"Trim the start to {_fmt(first)}?"
            + gap_note
        )
    if end_missing:
        return (
            f"Part of your selected period has no data "
            f"(latest available is {_fmt(last)}). "
            f"Trim the end to {_fmt(last)}?"
            + gap_note
        )
    # Interior gaps only — leading and trailing dates both present
    return f"{interior_count} day(s) missing within the selected period."


# ── Core selection + averaging ────────────────────────────────────────────────


def select_period(
    meter: MeterDataSet,
    from_md: MonthDay,
    to_md: MonthDay,
    years: list[int] | None = None,
) -> PeriodResolution:
    """Filter ``meter`` to the window/years, then average per ``(month, day)``.

    ``years`` ``None`` ⇒ all years present (matching months averaged together).
    """
    target = set(target_calendar_dates(from_md, to_md))
    year_set: set[int] | None = set(years) if years is not None else None

    e1_f = _filter(meter.e1, target, year_set)
    b1_f = _filter(meter.b1, target, year_set)

    if e1_f.empty:
        raise ValueError(
            "No meter data in the selected period and years."
        )

    notes: list[str] = []
    years_used: set[int] = set()

    new_e1, avg_md_e1 = _average(e1_f, years_used, notes, "E1")
    new_b1, _ = _average(b1_f, years_used, notes, "B1")

    averaged = any(
        count > 1 for count in _per_md_year_counts(e1_f).values()
    )

    if averaged:
        combined_years = sorted(years_used)
        notes.append(
            "Averaged matching calendar days across years: "
            + ", ".join(str(y) for y in combined_years)
            + "."
        )

    if not new_e1.empty:
        dates = new_e1.index.date
        start_date = min(dates)
        end_date = max(dates)
        effective_start_md = (start_date.month, start_date.day)
        effective_end_md = (end_date.month, end_date.day)
        period_days = len(set(dates))
    else:
        start_date = meter.start_date
        end_date = meter.end_date
        effective_start_md = from_md
        effective_end_md = to_md
        period_days = 0

    new_meter = MeterDataSet(
        e1=new_e1,
        b1=new_b1,
        nmi=meter.nmi,
        start_date=start_date,
        end_date=end_date,
        warnings=list(meter.warnings) + notes,
    )

    return PeriodResolution(
        meter=new_meter,
        period_days=period_days,
        effective_start_md=effective_start_md,
        effective_end_md=effective_end_md,
        averaged=averaged,
        years_used=sorted(years_used),
        notes=notes,
    )


# ── Private helpers ───────────────────────────────────────────────────────────


def _ref_date(md: MonthDay, year: int = _REF_LEAP_YEAR) -> datetime.date:
    """Construct a reference-year date for ``(month, day)``; raises on invalid."""
    return datetime.date(year, md[0], md[1])


def _fmt(md: MonthDay) -> str:
    """Format ``(month, day)`` as ``dd/mm``."""
    return f"{md[1]}/{md[0]}"


def _filter(
    df: pd.DataFrame, target: set[MonthDay], year_set: set[int] | None
) -> pd.DataFrame:
    """Keep rows whose ``(month, day)`` is in ``target`` and year in ``year_set``."""
    if df.empty:
        return df
    dates = df.index.date
    mask = np.array(
        [
            (d.month, d.day) in target and (year_set is None or d.year in year_set)
            for d in dates
        ],
        dtype=bool,
    )
    return df[mask]


def _per_md_year_counts(df: pd.DataFrame) -> dict[MonthDay, int]:
    """Number of contributing (distinct-year) days per ``(month, day)``."""
    counts: dict[MonthDay, set[int]] = defaultdict(set)
    for d in df.index.date:
        counts[(d.month, d.day)].add(d.year)
    return {md: len(ys) for md, ys in counts.items()}


def _average(
    df: pd.DataFrame,
    years_used: set[int],
    notes: list[str],
    stream: str,
) -> tuple[pd.DataFrame, set[MonthDay]]:
    """Average ``df`` per ``(month, day)`` by wall-clock time alignment.

    Returns the new DataFrame and the set of ``(month, day)`` it contains.
    ``years_used`` and ``notes`` are mutated in place (only ``E1`` feeds notes
    to avoid duplicates — the caller only passes ``B1`` for solar averaging).

    The output index is drawn from the earliest contributing year's actual
    timestamps, so spring-forward days produce 46-slot output and normal days
    produce 48-slot output. Slots absent in a given year contribute NaN to the
    mean (excluded) rather than zero.
    """
    if df.empty:
        empty = pd.DataFrame(columns=["kwh"])
        empty.index = pd.DatetimeIndex([], tz=MELBOURNE_TZ)
        return empty, set()

    # (month, day) -> list of (year, sorted day-DataFrame)
    by_md: dict[MonthDay, list[tuple[int, pd.DataFrame]]] = defaultdict(list)
    for date in sorted(set(df.index.date)):
        day_rows = df[df.index.date == date].sort_index()
        by_md[(date.month, date.day)].append((date.year, day_rows))

    timestamps: list[pd.Timestamp] = []
    values: list[float] = []
    md_present: set[MonthDay] = set()

    for md in sorted(by_md.keys(), key=lambda m: (m[0], m[1])):
        entries = sorted(by_md[md], key=lambda e: e[0])
        for year, _ in entries:
            years_used.add(year)

        # Canonical index = earliest year's real timestamps, deduplicated by
        # wall-clock time (guards against shift_forward artefacts in test data).
        _, ref_day = entries[0]
        seen_times: set = set()
        canonical_ts: list[pd.Timestamp] = []
        for ts in ref_day.index:
            t = ts.time()
            if t not in seen_times:
                seen_times.add(t)
                canonical_ts.append(ts)
        canonical_idx = pd.DatetimeIndex(canonical_ts)
        canonical_times = [ts.time() for ts in canonical_idx]

        # Align each year to the canonical time-of-day grid; NaN for absent slots.
        rows: list[np.ndarray] = []
        for _, day_df in entries:
            time_to_val: dict = {}
            for ts, val in zip(day_df.index, day_df["kwh"]):
                t = ts.time()
                if t not in time_to_val:
                    time_to_val[t] = val
            row = np.array([time_to_val.get(t, np.nan) for t in canonical_times])
            rows.append(row)

        mean_vec = np.nanmean(np.vstack(rows), axis=0)

        timestamps.extend(canonical_idx)
        values.extend(mean_vec.tolist())
        md_present.add(md)

        if stream == "E1" and len(entries) > 1:
            yrs = ", ".join(str(y) for y, _ in entries)
            notes.append(
                f"{md[1]}/{md[0]}: averaged {len(entries)} years ({yrs})."
            )

    result = pd.DataFrame({"kwh": values}, index=pd.DatetimeIndex(timestamps))
    return result, md_present
