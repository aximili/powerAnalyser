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
