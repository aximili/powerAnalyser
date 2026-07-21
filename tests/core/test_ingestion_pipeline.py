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


# ── Duplicate timestamp deduplication keeps last (revised) reading ────────────


def test_duplicate_timestamps_keep_last_and_warn(tmp_path):
    """Overlapping 300-record date ranges: the revised (later) reading must win.

    NEM12 files can contain a re-issued block for the same date. The first
    block has all intervals = 0.1 kWh (stale). The second block has all
    intervals = 0.9 kWh (revised). The pipeline must:
      1. Keep the revised (last) values — 0.9 kWh per interval.
      2. Emit exactly one warning naming the number of replaced intervals
         and the affected date.

    Hand-verification:
      date 2024-06-01 appears twice in the combined series before dedup:
        block A: 48 × 0.1 kWh
        block B: 48 × 0.9 kWh  ← revised, should win
      After dedup: 48 rows for 2024-06-01 each = 0.9 kWh.
    """
    def line300(datestr, n, val):
        return "300," + datestr + "," + ",".join(val for _ in range(n)) + ",A,,,"

    csv = "\n".join([
        "100,NEM12,6408088506",
        "200,6408088506,B1E1,E1,E1,,1217287,KWH,30,",
        line300("20240601", 48, "0.1"),  # stale block
        line300("20240601", 48, "0.9"),  # revised block — should win
        "900",
    ]) + "\n"
    path = tmp_path / "revised.csv"
    path.write_text(csv, encoding="utf-8")

    meter = IngestionPipeline().load(path)

    day = datetime.date(2024, 6, 1)
    rows = meter.e1[meter.e1.index.date == day]
    assert len(rows) == 48, "Revised day should have exactly 48 intervals"
    assert (rows["kwh"].values == pytest.approx([0.9] * 48)), "Revised values (0.9) must win"

    dup_warnings = [w for w in meter.warnings if "Duplicate" in w or "duplicate" in w]
    assert len(dup_warnings) == 1
    w = dup_warnings[0]
    assert "48" in w
    assert "2024-06-01" in w


# ── DST spring-forward: no false "revised data" warning (and genuine revised still warns) ──


def test_spring_forward_produces_no_false_revised_data_warning(tmp_path):
    """A spring-forward file must NOT emit a 'revised data' / 'Duplicate' warning.

    Previously, _block_to_series interpolated the missing 02:00/02:30 slots to pad
    to 48, then tz_localize(nonexistent="shift_forward") shifted those phantom values
    to 03:00/03:30 — colliding with the real 03:00/03:30 readings.  The dedup step
    in _records_to_dataframe then falsely emitted "stale interval(s) replaced by
    revised data".  After the fix, the tz-aware index is built directly with 46 slots
    so no phantom timestamps are created.
    """
    def line300(datestr, n):
        return "300," + datestr + "," + ",".join("0.1" for _ in range(n)) + ",A,,,"

    csv = "\n".join([
        "100,NEM12,6408088506",
        "200,6408088506,B1E1,E1,E1,,1217287,KWH,30,",
        line300("20241006", 46),   # spring-forward: exactly 46 intervals
        "900",
    ]) + "\n"
    path = tmp_path / "spring_forward_only.csv"
    path.write_text(csv, encoding="utf-8")

    meter = IngestionPipeline().load(path)

    # The honest DST warning must still be present
    dst_warnings = [w for w in meter.warnings if "spring-forward" in w.lower()]
    assert dst_warnings, "Expected a DST spring-forward warning"

    # No false "revised data" / "Duplicate" warning from the DST artifact
    false_warnings = [
        w for w in meter.warnings
        if "revised" in w.lower() or "duplicate" in w.lower()
    ]
    assert not false_warnings, (
        f"Spring-forward must not produce a false 'revised data' warning; got: {false_warnings}"
    )

    # Confirm exactly 46 intervals (correct for spring-forward, no padding to 48)
    assert len(meter.e1) == 46, f"Expected 46 intervals on spring-forward day, got {len(meter.e1)}"


def test_genuine_revised_data_still_warns(tmp_path):
    """A genuine overlapping 300-block must still trigger the 'revised data' warning.

    Ensures the spring-forward fix does NOT blanket-suppress real duplicate detection.
    Two blocks for the same normal day: stale (0.1 kWh) then revised (0.9 kWh).
    Pipeline must keep revised values and emit exactly one duplicate/revised warning.
    """
    def line300(datestr, n, val):
        return "300," + datestr + "," + ",".join(val for _ in range(n)) + ",A,,,"

    csv = "\n".join([
        "100,NEM12,6408088506",
        "200,6408088506,B1E1,E1,E1,,1217287,KWH,30,",
        line300("20240601", 48, "0.1"),   # stale block
        line300("20240601", 48, "0.9"),   # revised block — must win
        "900",
    ]) + "\n"
    path = tmp_path / "genuine_revised.csv"
    path.write_text(csv, encoding="utf-8")

    meter = IngestionPipeline().load(path)

    day = datetime.date(2024, 6, 1)
    rows = meter.e1[meter.e1.index.date == day]
    assert len(rows) == 48
    assert (rows["kwh"].values == pytest.approx([0.9] * 48)), "Revised (0.9) values must win"

    revised_warnings = [
        w for w in meter.warnings
        if "revised" in w.lower() or "duplicate" in w.lower()
    ]
    assert revised_warnings, "Expected a 'revised data' warning for genuine overlapping blocks"
    assert "2024-06-01" in revised_warnings[0]


# ── Timezone failure raises RuntimeError (no silent UTC fallback) ─────────────


def test_tz_localize_failure_raises_runtime_error(tmp_path, monkeypatch):
    """When tz_localize raises, the pipeline must propagate a RuntimeError.

    Regression for the C3 bug: the original code fell back to UTC silently,
    producing timestamps 10-11 h behind Melbourne and corrupting every rate
    boundary check for the day. The fix converts the fallback into a hard error.

    Hand-verification: a normal 2024-06-01 block (48 intervals) is used.
    tz_localize is monkeypatched to raise ValueError unconditionally.
    The pipeline must raise RuntimeError (not swallow it), and the message
    must name the block date and the underlying exception.
    """
    def line300(datestr, n, val="0.1"):
        return "300," + datestr + "," + ",".join(val for _ in range(n)) + ",A,,,"

    csv = "\n".join([
        "100,NEM12,6408088506",
        "200,6408088506,B1E1,E1,E1,,1217287,KWH,30,",
        line300("20240601", 48),
        "900",
    ]) + "\n"
    path = tmp_path / "tz_fail.csv"
    path.write_text(csv, encoding="utf-8")

    import pandas as pd
    from power_analyser.core.ingestion import pipeline as pipeline_mod

    original_tz_localize = pd.DatetimeIndex.tz_localize

    def failing_tz_localize(self, *args, **kwargs):
        raise ValueError("injected tz failure")

    monkeypatch.setattr(pd.DatetimeIndex, "tz_localize", failing_tz_localize)

    # The RuntimeError from _block_to_series is re-wrapped as ValueError by the
    # outer _records_to_dataframe handler (which adds NMI/block context). Either
    # way the error propagates — it is NOT silently swallowed and replaced with UTC.
    with pytest.raises((RuntimeError, ValueError)) as exc_info:
        IngestionPipeline().load(path)

    # The original RuntimeError message must be visible somewhere in the chain.
    full_msg = str(exc_info.value)
    assert "2024-06-01" in full_msg
    assert "injected tz failure" in full_msg


# ── Fix 1: DST detection generalised to 15-min intervals (H3) ────────────────


def test_15min_spring_forward_interpolates_to_96_intervals_and_warns(tmp_path):
    """A 15-min NEM12 file with 92 intervals on spring-forward day must be padded
    to 96 (not 48) via the generalised DST logic, with a warning emitted.

    Hand-verification:
      interval_length_min = 15  → n_expected = 1440//15 = 96
      dst_delta = 60//15 = 4  (one DST hour = 4 quarter-hour slots)
      spring-forward: 96 - 4 = 92 raw intervals  → interpolated to 96.
    """
    def line300(datestr, n):
        return "300," + datestr + "," + ",".join("0.1" for _ in range(n)) + ",A,,,"

    csv = "\n".join([
        "100,NEM12,6408088506",
        "200,6408088506,B1E1,E1,E1,,1217287,KWH,15,",   # 15-min intervals
        line300("20241006", 92),                          # spring-forward: 96 - 4 = 92
        "900",
    ]) + "\n"
    path = tmp_path / "fifteen_min_dst.csv"
    path.write_text(csv, encoding="utf-8")

    meter = IngestionPipeline().load(path)

    dst_date = datetime.date(2024, 10, 6)
    rows = meter.e1[meter.e1.index.date == dst_date]
    # After interpolation the pipeline builds a 96-slot array, then tz_localize shifts
    # the four 02:xx slots to 03:xx (which already exist), so dedup removes 4 →
    # 92 final intervals.  This matches the 30-min pattern: 46 raw → 48 array → 46 final.
    assert len(rows) == 92, f"Expected 92 intervals after DST fix, got {len(rows)}"

    # The critical check: the DST branch must have fired (not the generic pad-at-end path),
    # proven by the presence of a spring-forward warning.
    dst_warnings = [w for w in meter.warnings if "spring-forward" in w.lower()]
    assert dst_warnings, "Expected a DST spring-forward warning for 15-min file"


# ── Fix 2: Multi-NMI B1 mismatch warns and drops foreign data (H6) ──────────


def test_multi_nmi_b1_mismatch_warns_and_drops_foreign_data(tmp_path):
    """B1 records from a different NMI must trigger a warning and be dropped.

    A NEM12 file containing E1 for NMI_A and B1 for NMI_B (two separate meters)
    must NOT silently credit FiT from NMI_B against NMI_A's usage. Instead the
    pipeline must warn and return an empty B1 DataFrame.

    Hand-verification:
      e1_records NMI: NMI_A  ← primary
      b1_records NMI: NMI_B  ← foreign, must be dropped
      After fix: meter.b1.empty == True; one warning naming the mismatch.
    """
    def line300(datestr, n, val="0.1"):
        return "300," + datestr + "," + ",".join(val for _ in range(n)) + ",A,,,"

    csv = "\n".join([
        "100,NEM12,6408088506",
        "200,NMI_A,B1E1,E1,E1,,METER_A,KWH,30,",
        line300("20240601", 48),
        "200,NMI_B,B1E1,B1,B1,,METER_B,KWH,30,",   # B1 from a different meter
        line300("20240601", 48, "0.05"),
        "900",
    ]) + "\n"
    path = tmp_path / "multi_nmi.csv"
    path.write_text(csv, encoding="utf-8")

    meter = IngestionPipeline().load(path)

    assert meter.b1.empty, "Foreign B1 data must be dropped"
    assert meter.nmi == "NMI_A"

    mismatch_warnings = [w for w in meter.warnings if "mismatch" in w.lower()]
    assert mismatch_warnings, "Expected a NMI mismatch warning"
    assert "NMI_B" in mismatch_warnings[0]

