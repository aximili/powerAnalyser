"""NEM12 file parser.

Reads a NEM12-formatted CSV and returns a list of NMIRecord objects.
Only record types 100, 200, 300, and 900 are handled; 400/500 records are
intentionally skipped — they carry event overrides that are outside scope.

NEM12 format reference:
  100  Header
  200  NMI data details  (starts a new stream)
  300  Interval data     (one calendar day of readings)
  400  Interval events   (skipped)
  500  B2B details       (skipped)
  900  End of data
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path

from .models import IntervalBlock, NMIRecord

logger = logging.getLogger(__name__)

# Quality method codes that indicate non-actual data
ESTIMATED_QUALITY_CODES = frozenset({"E", "S", "N", "F"})


def parse_nem12(path: Path) -> list[NMIRecord]:
    """Parse a NEM12 file and return one NMIRecord per NMI/suffix combination.

    The returned list preserves file order. Callers can group by suffix
    (e.g., E1 vs B1) using a simple dict comprehension.
    """
    records: list[NMIRecord] = []
    current: NMIRecord | None = None

    with open(path, newline="", encoding="utf-8-sig") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            fields = line.split(",")
            record_type = fields[0].strip()

            if record_type == "100":
                pass  # Header — nothing actionable

            elif record_type == "200":
                current = _parse_200(fields)
                records.append(current)

            elif record_type == "300":
                if current is None:
                    logger.warning("300 record encountered before any 200 record — skipping")
                    continue
                block = _parse_300(fields, current.nmi, current.suffix)
                current.blocks.append(block)

            elif record_type in ("400", "500"):
                pass  # Event / B2B records — not needed for cost calculations

            elif record_type == "900":
                break  # End of file

            else:
                logger.debug("Unknown NEM12 record type %r — skipping", record_type)

    return records


# ── Private helpers ────────────────────────────────────────────────────────────

def _parse_200(fields: list[str]) -> NMIRecord:
    """Create an NMIRecord from a record-200 field list."""
    # Field positions per the NEM12 spec:
    #   0=RecordIndicator, 1=NMI, 2=NMIConfiguration, 3=RegisterID,
    #   4=NMISuffix, 5=MDMDataStreamIdentifier, 6=MeterSerialNumber,
    #   7=UOM, 8=IntervalLength, 9=NextScheduledReadDate
    nmi = fields[1].strip() if len(fields) > 1 else ""
    suffix = fields[4].strip() if len(fields) > 4 else ""
    uom = fields[7].strip() if len(fields) > 7 else "kWh"
    try:
        interval_length = int(fields[8].strip()) if len(fields) > 8 and fields[8].strip() else 30
    except ValueError:
        interval_length = 30
    return NMIRecord(nmi=nmi, suffix=suffix, uom=uom, interval_length_min=interval_length)


def _parse_300(fields: list[str], nmi: str, suffix: str) -> IntervalBlock:
    """Extract interval values from a record-300 field list.

    The interval values sit between field index 2 and the first non-numeric
    field (the quality method code). This naturally handles DST days where
    the interval count differs from the nominal 48.
    """
    # fields[1] = IntervalDate (YYYYMMDD)
    try:
        date = datetime.datetime.strptime(fields[1].strip(), "%Y%m%d").date()
    except (ValueError, IndexError):
        date = datetime.date.min
        logger.warning("Could not parse interval date from field %r", fields[1] if len(fields) > 1 else "")

    intervals: list[float] = []
    quality_method = "A"

    for raw in fields[2:]:
        value = raw.strip()
        if value == "":
            # Empty string means null/missing interval — treat as zero
            intervals.append(0.0)
            continue
        try:
            intervals.append(float(value))
        except ValueError:
            # First non-numeric field is the quality method code
            quality_method = value
            break

    return IntervalBlock(
        nmi=nmi,
        suffix=suffix,
        date=date,
        intervals=intervals,
        quality_method=quality_method,
    )
