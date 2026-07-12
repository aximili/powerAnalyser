"""Typed data containers for NEM12 parsed records.

These are plain dataclasses — no parsing logic lives here.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field


@dataclass
class IntervalBlock:
    """One day's interval readings for a single NMI/suffix combination.

    A NEM12 record-300 maps 1-to-1 onto an IntervalBlock.
    On a DST spring-forward day the intervals list will contain 46 values
    instead of the usual 48; on a fall-back day it will contain 50.
    """

    nmi: str
    suffix: str           # E1 (import/consumption) or B1 (export/solar)
    date: datetime.date
    intervals: list[float]   # kWh per interval (30-min default)
    quality_method: str   # A=actual, E=estimated, S=substituted, N=null


@dataclass
class NMIRecord:
    """All interval data for one NMI+suffix stream within a NEM12 file."""

    nmi: str
    suffix: str
    uom: str                  # Unit of measure, typically kWh
    interval_length_min: int  # Interval duration (5, 15, or 30 minutes)
    blocks: list[IntervalBlock] = field(default_factory=list)
