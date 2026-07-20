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

        # Validate NMI consistency in the E1 stream
        e1_nmis = {r.nmi for r in e1_records}
        if len(e1_nmis) > 1:
            raise ValueError(
                f"E1 stream spans multiple NMIs {sorted(e1_nmis)}. "
                "Each NMI must be loaded from its own NEM12 file."
            )

        nmi = e1_records[0].nmi

        all_warnings: list[str] = []

        # Validate B1 NMI matches primary E1 NMI; drop foreign records and warn
        if b1_records:
            mismatched_b1 = [r for r in b1_records if r.nmi != nmi]
            if mismatched_b1:
                foreign_nmis = sorted({r.nmi for r in mismatched_b1})
                all_warnings.append(
                    f"B1 stream NMI mismatch: expected '{nmi}' but found {foreign_nmis}. "
                    "Mismatched B1 data dropped — FiT credits not applied for foreign meter."
                )
                b1_records = [r for r in b1_records if r.nmi == nmi]

        e1_df, w = _records_to_dataframe(e1_records, E1_SUFFIX)
        all_warnings.extend(w)

        b1_df = _empty_kwh_frame()
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


def _empty_kwh_frame() -> pd.DataFrame:
    """An empty ``kwh`` DataFrame with a tz-aware Melbourne ``DatetimeIndex``.

    Using a ``DatetimeIndex`` (instead of the default ``RangeIndex`` you get
    from ``pd.DataFrame(columns=[...])``) keeps ``.index.date`` / ``.index.year``
    safe on empty streams — e.g. a file whose E1/B1 ``200`` record has no
    interval data, or a file with no solar (B1) stream at all.
    """
    idx = pd.DatetimeIndex([], tz=MELBOURNE_TZ)
    return pd.DataFrame({"kwh": pd.Series(dtype=float)}, index=idx)


def _records_to_dataframe(
    records: list[NMIRecord], suffix: str
) -> tuple[pd.DataFrame, list[str]]:
    """Flatten all IntervalBlocks from a list of NMIRecords into a single DataFrame."""
    all_series: list[pd.Series] = []
    warnings: list[str] = []

    for record in records:
        for block in record.blocks:
            try:
                series, w = _block_to_series(block, record.interval_length_min)
            except Exception as exc:
                # Surface exactly which NMI/suffix/date broke so a parse failure
                # on a large file is pinpointable instead of a bare exception.
                raise ValueError(
                    f"Failed to convert {block.suffix} block for NMI {block.nmi} "
                    f"on {block.date} ({len(block.intervals)} intervals): {exc}"
                ) from exc
            all_series.append(series)
            warnings.extend(w)

    if not all_series:
        return _empty_kwh_frame(), warnings

    # Stable sort preserves concat order for equal timestamps, so a later block
    # (revised data) stays after an earlier block (stale data) in the sorted
    # result. keep="last" then correctly retains the revised reading.
    combined = pd.concat(all_series).sort_index(kind="stable")
    # NEM12 files may contain revised date ranges where a later block corrects an
    # earlier one. keep="last" ensures revised readings win over stale ones.
    dup_mask = combined.index.duplicated(keep="last")
    n_dropped = int(dup_mask.sum())
    if n_dropped > 0:
        dropped_dates = sorted({ts.date() for ts in combined.index[dup_mask]})
        warnings.append(
            f"Duplicate timestamps: {n_dropped} stale interval(s) replaced by revised "
            f"data on {', '.join(str(d) for d in dropped_dates)}."
        )
    combined = combined[~dup_mask]
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
    dst_delta = 60 // interval_min   # intervals in one DST hour (2 for 30-min, 4 for 15-min)
    missing_pos = 2 * dst_delta      # 0-based index of the 02:00 slot
    warnings: list[str] = []
    intervals = list(block.intervals)

    if n_actual == n_expected - dst_delta:
        # Spring-forward: dst_delta intervals missing around 02:00 local time.
        # Interpolate all missing slots as the average of the immediate neighbours.
        left = intervals[missing_pos - 1] if missing_pos > 0 else 0.0
        right = intervals[missing_pos] if missing_pos < len(intervals) else 0.0
        fill = (left + right) / 2
        intervals = intervals[:missing_pos] + [fill] * dst_delta + intervals[missing_pos:]
        warnings.append(
            f"{block.date} ({block.suffix}): DST spring-forward — "
            f"{n_actual} intervals found; interpolated {dst_delta} missing intervals at 02:00."
        )

    elif n_actual == n_expected + dst_delta:
        # Fall-back: the one-hour DST window (02:00 through 02:00+dst_delta-1 slots)
        # appears twice (AEDT then AEST). Sum each pair to preserve total energy.
        merged = [
            intervals[missing_pos + i] + intervals[missing_pos + dst_delta + i]
            for i in range(dst_delta)
        ]
        intervals = intervals[:missing_pos] + merged + intervals[missing_pos + 2 * dst_delta:]
        warnings.append(
            f"{block.date} ({block.suffix}): DST fall-back — "
            f"{n_actual} intervals found; aggregated duplicate 02:00 intervals."
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
        # nonexistent="shift_forward" handles spring-forward (02:00/02:30 don't exist).
        # ambiguous=False resolves fall-back ambiguity: on the April fall-back day the
        # merged 48-slot index has ambiguous 02:00/02:30 (already summed from the 50
        # raw intervals); we resolve them to non-DST (AEST). This preserves the
        # time-of-day used for tariff matching and keeps every day in Melbourne tz.
        # (ambiguous="infer" can't infer DST from a single isolated day and would
        # raise, forcing the UTC fallback below — which then breaks mixed-tz concat.)
        tz_index = index.tz_localize(
            MELBOURNE_TZ,
            ambiguous=False,
            nonexistent="shift_forward",
        )
    except Exception as exc:
        raise RuntimeError(
            f"tz_localize to {MELBOURNE_TZ!r} failed for {block.suffix} block on "
            f"{block.date}: {exc}. Billing cannot continue with a corrupted timezone."
        ) from exc

    return pd.Series(
        data=intervals[:n_expected],
        index=tz_index,
        name="kwh",
        dtype=float,
    ), warnings
