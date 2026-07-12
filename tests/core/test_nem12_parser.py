"""Tests for the NEM12 file parser."""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from power_analyser.core.nem12.parser import parse_nem12

from .conftest import SAMPLE_NEM12


def test_parse_returns_records():
    records = parse_nem12(SAMPLE_NEM12)
    assert len(records) == 2, "Expected one E1 record and one B1 record"


def test_e1_and_b1_suffixes_present():
    records = parse_nem12(SAMPLE_NEM12)
    suffixes = {r.suffix for r in records}
    assert "E1" in suffixes
    assert "B1" in suffixes


def test_nmi_is_consistent():
    records = parse_nem12(SAMPLE_NEM12)
    nmis = {r.nmi for r in records}
    assert len(nmis) == 1, "All records should share the same NMI"


def test_normal_day_has_48_intervals():
    records = parse_nem12(SAMPLE_NEM12)
    e1 = next(r for r in records if r.suffix == "E1")
    normal_blocks = [b for b in e1.blocks if b.date != datetime.date(2024, 10, 6)]
    for block in normal_blocks:
        assert len(block.intervals) == 48, (
            f"Normal day {block.date} should have 48 intervals, got {len(block.intervals)}"
        )


def test_dst_spring_forward_day_has_46_intervals():
    """Oct 6 2024 is Victorian DST spring-forward — the NEM12 should have 46 intervals."""
    records = parse_nem12(SAMPLE_NEM12)
    e1 = next(r for r in records if r.suffix == "E1")
    dst_day = datetime.date(2024, 10, 6)
    dst_blocks = [b for b in e1.blocks if b.date == dst_day]
    assert dst_blocks, "DST day block not found in E1 data"
    assert len(dst_blocks[0].intervals) == 46


def test_total_block_count():
    records = parse_nem12(SAMPLE_NEM12)
    e1 = next(r for r in records if r.suffix == "E1")
    assert len(e1.blocks) == 7, "Expected 7 days of E1 data"


def test_quality_method_is_preserved():
    records = parse_nem12(SAMPLE_NEM12)
    e1 = next(r for r in records if r.suffix == "E1")
    for block in e1.blocks:
        assert block.quality_method == "A", f"Expected quality A for {block.date}"


def test_non_existent_file_raises():
    with pytest.raises(FileNotFoundError):
        parse_nem12(Path("/non/existent/file.csv"))
