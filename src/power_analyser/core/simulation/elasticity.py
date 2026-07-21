"""Behavioural elasticity and load-shifting simulation.

Models the reality that consumption patterns change when there is a financial
incentive to shift discretionary load (pool pump, EV, washing machine) into
a cheaper or free time window.

The simulation:
  1. Identifies "shiftable" intervals matching the configured source windows.
  2. Reduces their kWh by the user-supplied shift_fraction.
  3. Redistributes the shifted volume evenly across the plan's target
     FreeWindow intervals.

The original E1 DataFrame is never mutated — a new DataFrame is returned.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..tariff.schema import ElectricityPlan, TimeRange

# Locale-independent weekday abbreviations matching the DayOfWeek literals in
# schema.py (pd.Timestamp.weekday(): 0=Mon … 6=Sun). Mirrors
# calculator._WEEKDAY_NAMES — never use strftime("%a"), which varies with the
# system LC_TIME locale and silently stops matching schedules on non-English
# systems.
_WEEKDAY_NAMES: list[str] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class SourceWindow:
    """A time window containing shiftable (discretionary) load."""

    schedule: list[TimeRange]
    shift_fraction: float = field(
        default=0.0,
        metadata={"description": "Fraction of load to move out (0.0–1.0)"},
    )

    def __post_init__(self) -> None:
        if not (0.0 <= self.shift_fraction <= 1.0):
            raise ValueError(f"shift_fraction must be between 0 and 1, got {self.shift_fraction}")


@dataclass
class ElasticityConfig:
    """Full configuration for one load-shifting scenario.

    ``target_window_name`` must match the name of a FreeWindow in the plan
    being simulated.  If no matching window is found the simulation returns
    the original data unchanged.
    """

    source_windows: list[SourceWindow]
    target_window_name: str


class LoadShiftSimulator:
    """Produces a modified E1 DataFrame that reflects behavioural load-shifting."""

    def simulate(
        self,
        e1: pd.DataFrame,
        plan: ElectricityPlan,
        config: ElasticityConfig,
    ) -> pd.DataFrame:
        """Return a new E1 DataFrame with load shifted per ``config``.

        If the plan contains no FreeWindow matching ``config.target_window_name``
        the original DataFrame is returned unchanged (not a copy).
        """
        target_window = next(
            (fw for fw in plan.free_windows if fw.name == config.target_window_name),
            None,
        )
        if target_window is None:
            return e1

        simulated = e1.copy()

        # Process each calendar day independently
        for date in pd.unique(simulated.index.normalize()):
            day_mask = simulated.index.normalize() == date
            day_index = simulated.index[day_mask]
            dow = _WEEKDAY_NAMES[date.weekday()]

            total_shifted_kwh = 0.0

            for source in config.source_windows:
                src_mask = _mask_schedule(day_index, dow, source.schedule)
                if not src_mask.any():
                    continue
                # Reduce source intervals and accumulate the shifted volume
                shift_amounts = simulated.loc[day_mask & src_mask.values, "kwh"] * source.shift_fraction
                total_shifted_kwh += float(shift_amounts.sum())
                simulated.loc[day_mask & src_mask.values, "kwh"] -= shift_amounts

            if total_shifted_kwh <= 0:
                continue

            # Spread the shifted load evenly across the target window intervals
            tgt_mask = _mask_schedule(day_index, dow, target_window.schedule)
            n_target = int(tgt_mask.sum())
            if n_target > 0:
                per_interval = total_shifted_kwh / n_target
                simulated.loc[day_mask & tgt_mask.values, "kwh"] += per_interval

        return simulated


# ── Private helpers ────────────────────────────────────────────────────────────


def _mask_schedule(
    index: pd.DatetimeIndex, dow: str, schedule: list[TimeRange]
) -> pd.Series:
    """Return a boolean Series selecting intervals matching the schedule.

    An empty schedule list is treated as matching all times.
    """
    if not schedule:
        return pd.Series(True, index=index)

    result = np.zeros(len(index), dtype=bool)
    times = np.array([ts.time() for ts in index])

    for tr in schedule:
        if dow not in tr.days:
            continue
        result |= _time_range_mask(times, tr)

    return pd.Series(result, index=index)


def _time_range_mask(times: np.ndarray, tr: TimeRange) -> np.ndarray:
    """Vectorised check: which times fall within the TimeRange?"""
    if tr.end > tr.start:
        # Normal range, e.g., 07:00–23:00
        return (times >= tr.start) & (times < tr.end)
    else:
        # Overnight range, e.g., 23:00–07:00
        return (times >= tr.start) | (times < tr.end)
