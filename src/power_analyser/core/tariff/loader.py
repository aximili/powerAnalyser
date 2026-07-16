"""Utility functions for loading and validating plan JSON files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from .schema import ElectricityPlan


def load_plan(path: Path) -> ElectricityPlan:
    """Load and validate a single plan JSON file.

    Raises ``ValidationError`` (pydantic) if the JSON is structurally invalid,
    and ``ValueError`` if tier cross-references are broken.
    """
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    return ElectricityPlan.model_validate(raw)


def save_plan(plan: ElectricityPlan, directory: Optional[Path] = None) -> Path:
    """Upsert *plan* to ``directory/{plan_id}.json``.

    Writing a plan whose ``plan_id`` matches an existing file overwrites it
    (an upsert keyed on ``plan_id``). The directory is created if missing.
    Returns the path that was written.

    ``directory`` defaults to ``<data_dir>/plans`` (see :class:`Config`), which
    is where the comparison engine and the Analyse tab look for plans.
    """
    if directory is None:
        from power_analyser.config import get_config

        directory = get_config().data_dir / "plans"
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{plan.plan_id}.json"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(plan.model_dump_json(indent=2))
    return path


def load_plans_dir(directory: Path) -> list[ElectricityPlan]:
    """Load all ``*.json`` files from ``directory`` as ElectricityPlan objects.

    Files that fail validation are skipped with a warning printed to stderr.
    Returns an empty list if the directory is empty or contains no JSON files.
    """
    import sys

    plans: list[ElectricityPlan] = []
    json_files = sorted(directory.glob("*.json"))

    for path in json_files:
        try:
            plans.append(load_plan(path))
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            print(f"WARNING: skipping {path.name} — {exc}", file=sys.stderr)

    return plans
