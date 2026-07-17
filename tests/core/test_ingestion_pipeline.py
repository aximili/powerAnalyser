"""Tests for the data ingestion and normalisation pipeline."""

from __future__ import annotations

import datetime

import pandas as pd
import pytest

from power_analyser.core.ingestion.pipeline import IngestionPipeline

from .conftest import SAMPLE_NEM12

MELBOURNE_TZ = "Australia/Melbourne"


@pytest.fixture(scope="module")
def meter():
    return IngestionPipeline().load(SAMPLE_NEM12)


def test_e1_and_b1_dataframes_present(meter):
    assert not meter.e1.empty
    assert not meter.b1.empty


def test_timezone_is_melbourne(meter):
    assert str(meter.e1.index.tz) == MELBOURNE_TZ
    assert str(meter.b1.index.tz) == MELBOURNE_TZ


def test_e1_has_kwh_column(meter):
    assert "kwh" in meter.e1.columns


def test_date_range_covers_7_days(meter):
    unique_dates = set(meter.e1.index.date)
    assert len(unique_dates) == 7


def test_each_normal_day_has_48_intervals(meter):
    dst_date = datetime.date(2024, 10, 6)
    for date in set(meter.e1.index.date):
        if date == dst_date:
            continue
        n = len(meter.e1[meter.e1.index.date == date])
        assert n == 48, f"Normal day {date} expected 48 intervals, got {n}"


def test_dst_day_has_46_intervals(meter):
    """Spring-forward day genuinely has 46 intervals (02:00-02:30 don't exist in local time)."""
    dst_date = datetime.date(2024, 10, 6)
    n = len(meter.e1[meter.e1.index.date == dst_date])
    assert n == 46, f"DST spring-forward day should have 46 intervals, got {n}"


def test_dst_warning_is_emitted(meter):
    """The pipeline should emit a warning about the spring-forward day."""
    dst_warnings = [w for w in meter.warnings if "spring-forward" in w.lower() or "2024-10-06" in w]
    assert dst_warnings, "Expected at least one DST-related warning"


def test_kwh_values_are_non_negative(meter):
    assert (meter.e1["kwh"] >= 0).all(), "E1 kWh should be non-negative"
    assert (meter.b1["kwh"] >= 0).all(), "B1 kWh should be non-negative"


def test_nmi_is_populated(meter):
    assert meter.nmi == "6123456789"


# ── Empty-stream index type (regression for "'RangeIndex' has no attribute date") ──


def test_empty_stream_has_datetime_index(tmp_path):
    """A 200 record with no interval data must still yield a tz-aware DatetimeIndex.

    Previously the empty fallback used a plain RangeIndex, so any ``.index.date``
    access (e.g. in the GUI's file-load callback, or report/calculator/period)
    crashed with ``'RangeIndex' object has no attribute 'date'``.
    """
    csv = (
        "100,NEM12,6408088506\n"
        "200,6408088506,B1E1,E1,E1,,1217287,KWH,30,\n"
        "900\n"
    )
    path = tmp_path / "empty_stream.csv"
    path.write_text(csv, encoding="utf-8")

    from power_analyser.core.ingestion.pipeline import MeterDataSet  # noqa: F401

    import pandas as pd

    # The E1 200 record exists but has no 300 data -> e1 is empty but must be a DatetimeIndex.
    # (load() doesn't raise because e1_records is non-empty.)
    meter_data = IngestionPipeline().load(path)
    assert isinstance(meter_data.e1.index, pd.DatetimeIndex)
    assert str(meter_data.e1.index.tz) == MELBOURNE_TZ
    # The attribute access that previously crashed must now succeed:
    assert list(meter_data.e1.index.date) == []


def test_no_solar_stream_b1_is_datetime_index(meter):
    """Even when B1 is empty, its index must be a DatetimeIndex (no RangeIndex footgun)."""
    import pandas as pd

    # Build a meter with an explicitly-empty B1 via the pipeline helper:
    from power_analyser.core.ingestion.pipeline import _empty_kwh_frame

    empty_b1 = _empty_kwh_frame()
    assert isinstance(empty_b1.index, pd.DatetimeIndex)
    assert hasattr(empty_b1.index, "date")
    assert str(empty_b1.index.tz) == MELBOURNE_TZ


# ── Fall-back DST day (April, 50 intervals) — regression for mixed-tz concat crash ──


def test_fall_back_dst_day_parses_without_mixed_tz(tmp_path):
    """A file containing the April fall-back day (50 intervals) must parse cleanly.

    Regression: ``tz_localize(ambiguous="infer")`` used to raise on the isolated
    fall-back day, silently fall back to UTC, and the subsequent mixed-tz
    ``pd.concat`` produced an object ``Index`` (not ``DatetimeIndex``) — crashing
    every ``.index.date`` access with ``'Index' object has no attribute 'date'``
    on any file spanning a full year.
    """
    import datetime as _dt

    import pandas as pd

    def line300(datestr, n):
        return "300," + datestr + "," + ",".join("0.1" for _ in range(n)) + ",A,,,"

    csv = "\n".join(
        [
            "100,NEM12,6408088506",
            "200,6408088506,B1E1,E1,E1,,1217287,KWH,30,",
            line300("20240405", 48),   # normal
            line300("20240407", 50),   # FALL-BACK (AEDT → AEST)
            line300("20241006", 46),   # SPRING-FORWARD (AEST → AEDT)
            "900",
        ]
    ) + "\n"
    path = tmp_path / "dst_year.csv"
    path.write_text(csv, encoding="utf-8")

    meter_data = IngestionPipeline().load(path)

    # The whole-period index must stay a tz-aware DatetimeIndex (no UTC fallback,
    # no object Index from a mixed-tz concat).
    assert isinstance(meter_data.e1.index, pd.DatetimeIndex)
    assert str(meter_data.e1.index.tz) == MELBOURNE_TZ

    # The fall-back day is present with a full 48-slot row set.
    fb = _dt.date(2024, 4, 7)
    assert fb in set(meter_data.e1.index.date)
    assert len(meter_data.e1[meter_data.e1.index.date == fb]) == 48

    # .index.date must not raise (the original symptom).
    assert isinstance(list(meter_data.e1.index.date), list)


# ── Short day (maintenance outage) — unexpected interval count ────────────────


def test_short_day_pads_missing_intervals_and_warns(tmp_path):
    """A day with fewer than 48 intervals must be zero-padded and flagged.

    Real-world trigger: a scheduled supply outage during which the meter logs
    nothing. Here, 8 hours = 16 missing half-hour intervals, so only 32 of 48
    readings are present. The pipeline must not crash; it pads the missing
    period to 0 kWh and emits an "unexpected interval count" warning so the
    outage is visible (and not silently billed as if the data were complete).
    """
    def line300(datestr, n, val="0.1"):
        return "300," + datestr + "," + ",".join(val for _ in range(n)) + ",A,,,"

    csv = (
        "\n".join(
            [
                "100,NEM12,6408088506",
                "200,6408088506,B1E1,E1,E1,,1217287,KWH,30,",
                line300("20240609", 48),   # normal full day
                line300("20240610", 32),   # outage: 16 intervals missing (8h)
                "900",
            ]
        )
        + "\n"
    )
    path = tmp_path / "outage.csv"
    path.write_text(csv, encoding="utf-8")

    meter = IngestionPipeline().load(path)

    outage = datetime.date(2024, 6, 10)
    rows = meter.e1[meter.e1.index.date == outage]

    # Padded back to a full 48-slot day so downstream code (.index.date, the
    # calculator) never sees a short row set.
    assert len(rows) == 48, "Short day must be padded back to 48 slots"
    # The 16 missing intervals are filled with 0.0 kWh (outage = no usage).
    assert int((rows["kwh"] == 0.0).sum()) == 16
    # The 32 recorded intervals are preserved (not zeroed or dropped).
    assert int((rows["kwh"] != 0.0).sum()) == 32

    # Exactly one data-quality warning, naming the outage date and the count.
    assert len(meter.warnings) == 1
    w = meter.warnings[0]
    assert "2024-06-10" in w
    assert "unexpected interval count" in w
    assert "32" in w


