"""Utility functions for loading and validating plan JSON files."""

from __future__ import annotations

import json
from pathlib import Path

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
