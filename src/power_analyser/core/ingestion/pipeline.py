"""Data ingestion and normalisation pipeline.

Converts a raw NEM12 file into a pair of timezone-aware pandas DataFrames
(one for E1 import / consumption, one for B1 export / solar) sharing an
identical datetime index in Australia/Melbourne local time.

DST handling:
  Spring-forward (Oct): 46 intervals on that day.
    → The 2 intervals at 02:00–02:30 don't exist; they are interpolated as
      the average of their neighbours and flagged in MeterDataSet.warnings.
  Fall-back (Apr): 50 intervals on that day.
    → The 2 intervals at 02:00–02:30 occur twice; the paired readings are
      summed and flagged in MeterDataSet.warnings.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ..nem12.parser import parse_nem12
from ..nem12.models import IntervalBlock, NMIRecord

logger = logging.getLogger(__name__)

MELBOURNE_TZ = "Australia/Melbourne"
E1_SUFFIX = "E1"
B1_SUFFIX = "B1"


@dataclass
class MeterDataSet:
    """Normalised, timezone-aware meter data for one NMI.

    ``e1``: import / consumption DataFrame (kWh per 30-min interval).
    ``b1``: export / solar DataFrame (kWh per 30-min interval).
    Both DataFrames share a DatetimeTZIndex in Australia/Melbourne time and
    a single column ``kwh``.  ``b1`` may be empty if the meter has no solar.
    """

    e1: pd.DataFrame
    b1: pd.DataFrame
    nmi: str
    start_date: datetime.date
    end_date: datetime.date
    warnings: list[str] = field(default_factory=list)


class IngestionPipeline:
    """Load a NEM12 file and return a normalised MeterDataSet."""

    def load(self, path: Path) -> MeterDataSet:
        """Parse ``path`` and return a fully normalised MeterDataSet."""
        records = parse_nem12(path)

        if not records:
            raise ValueError(f"No NMI records found in {path}")

        # Separate E1 and B1 streams (keep the first occurrence of each)
        e1_records = [r for r in records if r.suffix == E1_SUFFIX]
        b1_records = [r for r in records if r.suffix == B1_SUFFIX]

        if not e1_records:
            raise ValueError(f"No E1 (consumption) stream found in {path}")

        # Use the first NMI found as the primary identifier
        nmi = e1_records[0].nmi

        all_warnings: list[str] = []

        e1_df, w = _records_to_dataframe(e1_records, E1_SUFFIX)
        all_warnings.extend(w)

        b1_df = pd.DataFrame(columns=["kwh"])
        if b1_records:
            b1_df, w = _records_to_dataframe(b1_records, B1_SUFFIX)
            all_warnings.extend(w)

        start = e1_df.index.date.min() if not e1_df.empty else datetime.date.min
        end = e1_df.index.date.max() if not e1_df.empty else datetime.date.min

        return MeterDataSet(
            e1=e1_df,
            b1=b1_df,
            nmi=nmi,
            start_date=start,
            end_date=end,
            warnings=all_warnings,
        )


# ── Private helpers ─────────────────────────────────────────────────────────


def _records_to_dataframe(
    records: list[NMIRecord], suffix: str
) -> tuple[pd.DataFrame, list[str]]:
    """Flatten all IntervalBlocks from a list of NMIRecords into a single DataFrame."""
    all_series: list[pd.Series] = []
    warnings: list[str] = []

    for record in records:
        for block in record.blocks:
            series, w = _block_to_series(block, record.interval_length_min)
            all_series.append(series)
            warnings.extend(w)

    if not all_series:
        empty = pd.DataFrame(columns=["kwh"])
        return empty, warnings

    combined = pd.concat(all_series).sort_index()
    # Remove exact duplicates (same timestamp appearing in overlapping date ranges)
    combined = combined[~combined.index.duplicated(keep="first")]
    df = combined.to_frame(name="kwh")
    return df, warnings


def _block_to_series(
    block: IntervalBlock, interval_min: int
) -> tuple[pd.Series, list[str]]:
    """Convert one IntervalBlock to a timezone-aware pandas Series.

    Normalises the interval list to exactly ``1440 / interval_min`` values,
    handling DST spring-forward (46 intervals) and fall-back (50 intervals).
    """
    n_expected = 1440 // interval_min
    n_actual = len(block.intervals)
    warnings: list[str] = []
    intervals = list(block.intervals)

    if n_actual == n_expected - 2:
        # Spring-forward: 2 intervals missing around 02:00 local time
        # Interpolate as the average of immediate neighbours at position 4
        missing_pos = 4  # 0-based: 0*30min=00:00, …, 4*30min=02:00
        left = intervals[missing_pos - 1] if missing_pos > 0 else 0.0
        right = intervals[missing_pos] if missing_pos < len(intervals) else 0.0
        fill = (left + right) / 2
        intervals.insert(missing_pos, fill)
        intervals.insert(missing_pos + 1, fill)
        warnings.append(
            f"{block.date} ({block.suffix}): DST spring-forward — "
            f"{n_actual} intervals found; interpolated 2 missing intervals at 02:00–02:30."
        )

    elif n_actual == n_expected + 2:
        # Fall-back: 02:00 and 02:30 each appear twice (AEDT then AEST)
        # Sum the paired readings to preserve total energy
        dup = 4  # position of the first duplicate pair
        merged_1 = intervals[dup] + intervals[dup + 2]
        merged_2 = intervals[dup + 1] + intervals[dup + 3]
        intervals = intervals[:dup] + [merged_1, merged_2] + intervals[dup + 4:]
        warnings.append(
            f"{block.date} ({block.suffix}): DST fall-back — "
            f"{n_actual} intervals found; aggregated duplicate 02:00–02:30 intervals."
        )

    elif n_actual != n_expected:
        warnings.append(
            f"{block.date} ({block.suffix}): unexpected interval count "
            f"{n_actual} (expected {n_expected}). Padding/truncating."
        )
        if n_actual < n_expected:
            intervals.extend([0.0] * (n_expected - n_actual))
        else:
            intervals = intervals[:n_expected]

    # Build a regular datetime index starting at midnight of block.date
    naive_start = pd.Timestamp(block.date.year, block.date.month, block.date.day)
    index = pd.date_range(
        start=naive_start,
        periods=n_expected,
        freq=f"{interval_min}min",
    )

    try:
        # nonexistent="shift_forward" handles any remaining spring-forward edge
        # ambiguous="infer" handles fall-back ambiguity by context
        tz_index = index.tz_localize(
            MELBOURNE_TZ,
            ambiguous="infer",
            nonexistent="shift_forward",
        )
    except Exception as exc:
        warnings.append(
            f"{block.date} ({block.suffix}): tz_localize failed ({exc}); falling back to UTC."
        )
        tz_index = index.tz_localize("UTC")

    return pd.Series(
        data=intervals[:n_expected],
        index=tz_index,
        name="kwh",
        dtype=float,
    ), warnings
