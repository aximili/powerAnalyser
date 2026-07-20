"""Tests for ``core/tariff/loader.py`` (``load_plan``, ``load_plans_dir``, ``save_plan``).

These functions are the on-disk boundary for plan JSON: the comparison engine,
the Analyse tab, and the Manual-extraction persistence path all rely on them.
Previously had ZERO coverage.

Every test is offline and uses pytest's ``tmp_path`` for isolation — no writes
into the real ``data/plans/`` directory, no dependence on whatever plan JSONs
happen to ship today.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from power_analyser.core.tariff.loader import load_plan, load_plans_dir, save_plan
from power_analyser.core.tariff.schema import ElectricityPlan


# ── A representative plan dict with non-trivial Decimal fields ────────────────


def _rich_plan_dict() -> dict:
    """A plan exercising every Decimal-bearing field and every list field.

    Decimal fields are deliberately given values with >2 decimal places so a
    round-trip that lost precision (e.g. via float coercion) would be caught.
    Conditions / fit_tiers / free_windows / step_tariffs are all populated so
    the schema validator and the JSON serialiser both exercise non-trivial
    paths.
    """
    return {
        "plan_id": "rich_plan_round_trip",
        "retailer": "Round Trip Co",
        "plan_name": "Rich Plan",
        "valid_from": "2024-01-01",
        "valid_to": "2024-12-31",
        "last_updated": "2024-06-01T10:00:00+10:00",
        "conditions": ["Direct debit required", "Solar required"],
        "daily_supply_charge": "1.0780",  # 4 dp — must survive round-trip exactly
        "usage_tiers": [
            {
                "name": "Peak",
                "rate": "0.3850",  # 4 dp
                "schedule": [
                    {"days": ["Mon", "Tue", "Wed", "Thu", "Fri"], "start": "15:00", "end": "21:00"}
                ],
            },
            {"name": "Off-Peak", "rate": "0.1234", "schedule": []},  # 4 dp
        ],
        "free_windows": [
            {
                "name": "Midday",
                "schedule": [
                    {"days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], "start": "11:00", "end": "14:00"}
                ],
                "fair_use_cap_kwh": 50.0,
                "overflow_tier": "Off-Peak",
            }
        ],
        "fit_tiers": [
            {"name": "Flat FiT", "rate": "0.0670", "schedule": []}  # 4 dp
        ],
        "step_tariffs": [
            {"threshold_kwh_per_day": 15.0, "tier_below": "Off-Peak", "tier_above": "Peak"}
        ],
    }


# ── load_plan ────────────────────────────────────────────────────────────────


def test_load_plan_round_trips_all_decimal_fields(tmp_path: Path):
    """Every Decimal field must survive save → load with EXACT precision.

    Decimal is the money type end-to-end (calculator.py:13 design note); a
    float-coercion bug anywhere in the JSON path would silently corrupt rates.
    This test fails on any precision loss (>2 dp values, specifically).
    """
    plan = ElectricityPlan.model_validate(_rich_plan_dict())
    written_path = save_plan(plan, directory=tmp_path)
    assert written_path == tmp_path / "rich_plan_round_trip.json"

    reloaded = load_plan(written_path)

    # Scalar Decimal fields preserved exactly.
    assert reloaded.daily_supply_charge == Decimal("1.0780")
    assert reloaded.usage_tiers[0].rate == Decimal("0.3850")
    assert reloaded.usage_tiers[1].rate == Decimal("0.1234")
    assert reloaded.fit_tiers[0].rate == Decimal("0.0670")

    # Type is Decimal, not float (guards a regression to plain json float parse).
    assert isinstance(reloaded.daily_supply_charge, Decimal)
    for tier in reloaded.usage_tiers:
        assert isinstance(tier.rate, Decimal)

    # Non-Decimal fields round-trip too.
    assert reloaded.plan_id == "rich_plan_round_trip"
    assert reloaded.retailer == "Round Trip Co"
    assert reloaded.conditions == ["Direct debit required", "Solar required"]
    assert reloaded.last_updated == "2024-06-01T10:00:00+10:00"
    assert reloaded.step_tariffs[0].threshold_kwh_per_day == 15.0
    assert reloaded.free_windows[0].overflow_tier == "Off-Peak"


def test_load_plan_from_hand_authored_json(tmp_path: Path):
    """Hand-authored JSON (the ``data/plans/*.json`` shape) loads cleanly.

    This is the path ``data/plans/globird_four4free.json`` etc. take — a JSON
    file written by a human or by ``save_plan`` in a prior session, loaded by
    ``load_plan`` directly (NOT via the extractor, so ``last_updated`` is the
    file's value, not capture-time). Pins the AGENTS.md invariant: "Hand-
    authored ``load_plan`` JSON preserves the file value."
    """
    raw = _rich_plan_dict()
    path = tmp_path / "authored.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    plan = load_plan(path)

    assert plan.plan_id == "rich_plan_round_trip"
    assert plan.last_updated == "2024-06-01T10:00:00+10:00"  # preserved, not overwritten
    assert plan.daily_supply_charge == Decimal("1.0780")


def test_load_plan_malformed_json_raises_clear_error(tmp_path: Path):
    """Malformed JSON must raise ``json.JSONDecodeError`` (a ValueError subclass),
    not a bare/opaque exception.

    ``json.JSONDecodeError`` is a subclass of ``ValueError``, so matching on
    ``ValueError`` is sufficient and also catches any future wrapping. The
    key invariant is "typed, identifiable error" — a generic ``Exception`` or
    a silent ``None`` return would be a regression.
    """
    path = tmp_path / "broken.json"
    path.write_text("{ not valid json ((( ", encoding="utf-8")

    # JSONDecodeError is a subclass of ValueError; accept either so the test
    # does not over-couple to which exact type propagates from json.load.
    with pytest.raises(ValueError) as exc_info:
        load_plan(path)

    # Error message must mention the file or the parse problem (not be empty).
    assert str(exc_info.value)


def test_load_plan_structurally_invalid_raises_validation_error(tmp_path: Path):
    """Valid JSON that violates the schema (e.g. negative rate) raises
    ``ValidationError`` from pydantic — distinct from a JSON parse failure.

    Together with ``test_load_plan_malformed_json_raises_clear_error`` this
    documents the two error tiers: structural JSON errors → ValueError family;
    schema violations → ValidationError.
    """
    raw = {
        "plan_id": "bad",
        "retailer": "X",
        "plan_name": "Bad",
        "daily_supply_charge": "1.00",
        "usage_tiers": [{"name": "Flat", "rate": "-0.30", "schedule": []}],  # rate < 0
    }
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValidationError):
        load_plan(path)


def test_load_plan_broken_tier_reference_raises_value_error(tmp_path: Path):
    """A step/free-window referencing an unknown tier name raises ValueError.

    Pins schema.py:131 ``_validate_tier_references``. This is the second tier
    of "structurally valid JSON but semantically broken" — distinct from both
    a JSON parse error and a field-level ValidationError.
    """
    raw = {
        "plan_id": "dangling",
        "retailer": "X",
        "plan_name": "Dangling Ref",
        "daily_supply_charge": "1.00",
        "usage_tiers": [{"name": "Flat", "rate": "0.30", "schedule": []}],
        "step_tariffs": [
            {"threshold_kwh_per_day": 5.0, "tier_below": "DoesNotExist", "tier_above": "Flat"}
        ],
    }
    path = tmp_path / "dangling.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown"):
        load_plan(path)


# ── load_plans_dir ───────────────────────────────────────────────────────────


def test_load_plans_dir_loads_all_valid_json(tmp_path: Path):
    """All valid ``*.json`` files load; order is deterministic (sorted by name)."""
    save_plan(_flat_plan("alpha"), directory=tmp_path)
    save_plan(_flat_plan("beta"), directory=tmp_path)

    plans = load_plans_dir(tmp_path)

    assert {p.plan_id for p in plans} == {"alpha", "beta"}
    # Sorted by filename (loader.py:56), so alpha.json precedes beta.json.
    assert [p.plan_id for p in plans] == ["alpha", "beta"]


def test_load_plans_dir_skips_non_json_files(tmp_path: Path):
    """Non-JSON files (``*.txt``, ``*.md``, ``*.csv``) are ignored, not crashed on.

    ``load_plans_dir`` globs only ``*.json`` (loader.py:56), so a stray README
    or notes file in ``data/plans/`` must not break loading. This pins that
    behaviour against an accidental refactor to ``glob("*")``.
    """
    save_plan(_flat_plan("good"), directory=tmp_path)
    (tmp_path / "README.txt").write_text("notes", encoding="utf-8")
    (tmp_path / "notes.md").write_text("# plans", encoding="utf-8")
    (tmp_path / "data.csv").write_text("a,b\n1,2", encoding="utf-8")

    plans = load_plans_dir(tmp_path)

    assert len(plans) == 1
    assert plans[0].plan_id == "good"


def test_load_plans_dir_skips_invalid_json_without_crashing(tmp_path: Path,
                                                            capsys: pytest.CaptureFixture[str]):
    """A malformed/invalid JSON file is skipped with a stderr warning; the rest load.

    Pins loader.py:58-62: ``ValidationError``, ``ValueError`` and
    ``json.JSONDecodeError`` are all caught per-file. One bad file must NOT
    abort the whole directory load.
    """
    save_plan(_flat_plan("good"), directory=tmp_path)
    (tmp_path / "broken.json").write_text("{ totally broken ", encoding="utf-8")
    # Valid JSON but schema-invalid (negative rate).
    (tmp_path / "invalid.json").write_text(
        json.dumps(
            {
                "plan_id": "bad",
                "retailer": "X",
                "plan_name": "Bad",
                "daily_supply_charge": "1.00",
                "usage_tiers": [{"name": "Flat", "rate": "-0.30", "schedule": []}],
            }
        ),
        encoding="utf-8",
    )

    plans = load_plans_dir(tmp_path)

    # Only the one valid plan loads; the two bad files are skipped.
    assert [p.plan_id for p in plans] == ["good"]
    captured = capsys.readouterr()
    # Each skip emits a WARNING line naming the file (loader.py:62).
    assert "broken.json" in captured.err
    assert "invalid.json" in captured.err


def test_load_plans_dir_empty_directory_returns_empty_list(tmp_path: Path):
    """An empty directory (or one with no JSON) returns ``[]``, not an error."""
    assert load_plans_dir(tmp_path) == []
    # Even with non-JSON files only.
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    assert load_plans_dir(tmp_path) == []


# ── save_plan ────────────────────────────────────────────────────────────────


def test_save_plan_upserts_keyed_on_plan_id(tmp_path: Path):
    """Re-saving a plan with the same ``plan_id`` overwrites; no duplicate files.

    This is the upsert invariant AGENTS.md documents: "keyed on ``plan_id``,
    so re-extracting a plan updates the same file". The Manual-extraction path
    and the Analyse tab rely on it — a regression that created ``plan_id_1.json``
    etc. would silently fragment the plan store.
    """
    plan = _flat_plan("upsert_target")
    first_path = save_plan(plan, directory=tmp_path)
    assert first_path == tmp_path / "upsert_target.json"

    # Mutate the plan (same plan_id, different rate) and re-save.
    plan_v2 = ElectricityPlan.model_validate(
        {
            "plan_id": "upsert_target",
            "retailer": "Test Retailer",
            "plan_name": "V2",
            "daily_supply_charge": "1.00",
            "usage_tiers": [{"name": "Flat", "rate": "0.99", "schedule": []}],
        }
    )
    second_path = save_plan(plan_v2, directory=tmp_path)

    # Same path (keyed on plan_id), still exactly one file.
    assert second_path == first_path
    assert sorted(p.name for p in tmp_path.glob("*.json")) == ["upsert_target.json"]

    # The file content reflects the LATEST write (the upsert).
    reloaded = load_plan(second_path)
    assert reloaded.usage_tiers[0].rate == Decimal("0.99")
    assert reloaded.plan_name == "V2"


def test_save_plan_creates_directory_if_missing(tmp_path: Path):
    """``save_plan`` creates the target directory (and parents) if it does not exist.

    Pins loader.py:40 ``directory.mkdir(parents=True, exist_ok=True)``. The
    default-config path (``data/plans``) may not exist on a fresh checkout;
    ``save_plan`` must not require the caller to pre-create it.
    """
    nested = tmp_path / "deeply" / "nested" / "plans"
    assert not nested.exists()

    plan = _flat_plan("mkdir_test")
    written = save_plan(plan, directory=nested)

    assert nested.is_dir()
    assert written == nested / "mkdir_test.json"
    assert load_plan(written).plan_id == "mkdir_test"


def test_save_plan_round_trip_is_stable(tmp_path: Path):
    """save → load → save → load produces byte-identical files (idempotent upsert).

    Catches a class of bugs where a round-trip subtly mutates the model (e.g.
    drops an optional field, reorders lists, or coerces a Decimal). The plan
    store is repeatedly round-tripped by the extraction pipeline; instability
    would cause spurious diffs and re-writes.
    """
    plan = ElectricityPlan.model_validate(_rich_plan_dict())

    path1 = save_plan(plan, directory=tmp_path)
    bytes1 = path1.read_bytes()

    reloaded1 = load_plan(path1)
    path2 = save_plan(reloaded1, directory=tmp_path)
    bytes2 = path2.read_bytes()

    assert path1 == path2  # same plan_id → same path (upsert)
    assert bytes1 == bytes2, "save → load → save is not byte-stable"

    # And the second reload still matches the original model.
    reloaded2 = load_plan(path2)
    assert reloaded2.model_dump() == reloaded1.model_dump() == plan.model_dump()


def test_save_plan_default_directory_matches_engine_load_directory(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch):
    """When ``directory`` is omitted, ``save_plan`` writes to ``<data_dir>/plans`` —
    the SAME directory ``load_plans_dir`` and the comparison engine read from.

    Pins loader.py:35-38 (the ``get_config().data_dir / "plans"`` default).
    A regression that pointed the default elsewhere would break the
    Manual-extraction flow ("extracted plans flow into the Analyse tab
    automatically" — AGENTS.md).

    ``get_config`` is imported LAZILY inside ``save_plan``
    (``from power_analyser.config import get_config``), so the monkeypatch
    must target the source module ``power_analyser.config.get_config`` —
    patching ``loader.get_config`` would have no effect because that name is
    only bound at call time and resolves through the source module.
    """
    from power_analyser.config import Config

    # Redirect Config.data_dir to the isolated tmp tree so the test does not
    # write into the real data/plans directory.
    fake_cfg = Config(data_dir=tmp_path)
    monkeypatch.setattr("power_analyser.config.get_config", lambda: fake_cfg)

    plan = _flat_plan("default_dir_test")
    written = save_plan(plan)  # no directory argument → default path

    expected_dir = tmp_path / "plans"
    assert written == expected_dir / "default_dir_test.json"
    # The default directory is the one the engine reads from.
    assert load_plans_dir(expected_dir)[0].plan_id == "default_dir_test"


# ── helpers ───────────────────────────────────────────────────────────────────


def _flat_plan(plan_id: str) -> ElectricityPlan:
    """A minimal flat-rate plan, for tests that just need ANY valid plan."""
    return ElectricityPlan.model_validate(
        {
            "plan_id": plan_id,
            "retailer": "Test Retailer",
            "plan_name": f"Flat {plan_id}",
            "daily_supply_charge": "1.00",
            "usage_tiers": [{"name": "Flat", "rate": "0.30", "schedule": []}],
        }
    )
