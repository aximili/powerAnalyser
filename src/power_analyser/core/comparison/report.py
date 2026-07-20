"""Comparison and presentation layer.

Runs all plan/scenario permutations and assembles the results into a ranked
report with delta analysis and per-plan cost breakdowns.

Usage (programmatic):
    engine = ComparisonEngine()
    result = engine.compare(meter, plans, elasticity_configs)
    for entry in result.ranked:
        print(entry.plan_name, entry.baseline_net)

Usage (CLI smoke-test):
    python -m power_analyser.core.comparison.report \\
        --nem12 data/sample_nem12.csv \\
        --plans-dir data/plans/
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from ..ingestion.pipeline import IngestionPipeline, MeterDataSet
from ..simulation.calculator import CostCalculator, PeriodResult
from ..simulation.elasticity import ElasticityConfig, LoadShiftSimulator
from ..tariff.loader import load_plans_dir
from ..tariff.schema import ElectricityPlan


@dataclass
class ComparisonEntry:
    """Results for one plan, including optional load-shifted scenario."""

    plan_id: str
    plan_name: str
    retailer: str

    # Cost components (all Decimal, $ over the analysed period)
    baseline_supply: Decimal
    baseline_usage: Decimal
    baseline_solar_credit: Decimal
    baseline_net: Decimal

    # Promotional / free-window savings (informational)
    baseline_promotional_saving: Decimal

    # Load-shift scenario (None when no ElasticityConfig was provided for this plan)
    simulated_net: Decimal | None = None
    shift_saving: Decimal | None = None  # baseline_net - simulated_net (positive = saving)

    # Informational metadata forwarded from the plan (not used in cost math)
    last_updated: str | None = None  # ISO-8601 capture time
    conditions: list[str] = field(default_factory=list)  # eligibility notes


@dataclass
class ComparisonResult:
    """Full comparison across all plans, ordered cheapest-first."""

    ranked: list[ComparisonEntry]   # sorted by simulated_net (if present) else baseline_net, ascending
    warnings: list[str]            # DST or data quality warnings from ingestion
    period_days: int                # number of calendar days in the dataset
    nmi: str


class ComparisonEngine:
    """Orchestrates the full comparison pipeline."""

    def compare(
        self,
        meter: MeterDataSet,
        plans: list[ElectricityPlan],
        elasticity_configs: dict[str, ElasticityConfig] | None = None,
    ) -> ComparisonResult:
        """Run all plans against the meter data and return a ranked result.

        ``elasticity_configs`` maps plan_id → ElasticityConfig.  Plans not
        present in the dict are compared using baseline data only.
        """
        if not plans:
            raise ValueError("At least one plan is required for comparison.")

        calculator = CostCalculator()
        simulator = LoadShiftSimulator()
        configs = elasticity_configs or {}
        entries: list[ComparisonEntry] = []

        for plan in plans:
            baseline: PeriodResult = calculator.calculate_period(meter, plan)

            simulated_net: Decimal | None = None
            shift_saving: Decimal | None = None

            if plan.plan_id in configs:
                config = configs[plan.plan_id]
                shifted_e1 = simulator.simulate(meter.e1, plan, config)
                sim_result: PeriodResult = calculator.calculate_period(
                    meter, plan, e1_override=shifted_e1
                )
                simulated_net = sim_result.total_net
                shift_saving = baseline.total_net - sim_result.total_net

            entries.append(
                ComparisonEntry(
                    plan_id=plan.plan_id,
                    plan_name=plan.plan_name,
                    retailer=plan.retailer,
                    last_updated=plan.last_updated,
                    conditions=list(plan.conditions),
                    baseline_supply=baseline.total_supply,
                    baseline_usage=baseline.total_usage,
                    baseline_solar_credit=baseline.total_solar_credit,
                    baseline_net=baseline.total_net,
                    baseline_promotional_saving=baseline.total_promotional_saving,
                    simulated_net=simulated_net,
                    shift_saving=shift_saving,
                )
            )

        ranked = sorted(entries, key=lambda e: e.simulated_net if e.simulated_net is not None else e.baseline_net)

        period_days = len(set(meter.e1.index.date)) if not meter.e1.empty else 0

        return ComparisonResult(
            ranked=ranked,
            warnings=list(meter.warnings),
            period_days=period_days,
            nmi=meter.nmi,
        )


# ── CLI entry point ────────────────────────────────────────────────────────────


def _parse_cli_md(text: str) -> tuple[int, int]:
    """Parse a CLI ``dd/mm`` (or ``dd/mm/yyyy``) arg into ``(month, day)``."""
    import datetime

    parts = text.strip().split("/")
    if len(parts) < 2:
        raise ValueError("--from/--to must be day/month, e.g. 1/6")
    try:
        day, month = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError("--from/--to must be day/month, e.g. 1/6") from exc
    try:
        datetime.date(2000, month, day)  # leap ref year
    except ValueError as exc:
        raise ValueError(f"{day}/{month} is not a valid date") from exc
    return (month, day)


def cli_main(argv: list[str] | None = None) -> None:
    """Print a ranked cost table from NEM12 + plans directory."""
    parser = argparse.ArgumentParser(
        description="Compare electricity plans against a NEM12 smart-meter file."
    )
    parser.add_argument("--nem12", required=True, type=Path, help="Path to NEM12 CSV file")
    parser.add_argument("--plans-dir", required=True, type=Path, help="Directory of plan JSON files")
    parser.add_argument(
        "--from",
        dest="from_md",
        default=None,
        help="Analysis window start as dd/mm (day/month). Omit for the full file range.",
    )
    parser.add_argument(
        "--to",
        dest="to_md",
        default=None,
        help="Analysis window end as dd/mm (day/month). Omit for the full file range.",
    )
    parser.add_argument(
        "--year",
        default="all",
        help="all (default, averages matching months across years) or a single YYYY.",
    )
    args = parser.parse_args(argv)

    pipeline = IngestionPipeline()
    try:
        meter = pipeline.load(args.nem12)
    except (ValueError, FileNotFoundError) as exc:
        print(f"ERROR loading NEM12 file: {exc}", file=sys.stderr)
        sys.exit(1)

    plans = load_plans_dir(args.plans_dir)
    if not plans:
        print(f"No valid plan JSON files found in {args.plans_dir}", file=sys.stderr)
        sys.exit(1)

    # Optional period selection + multi-year averaging.
    resolution_notes: list[str] = []
    if args.from_md or args.to_md:
        from ..ingestion.period import select_period
        try:
            from_md = _parse_cli_md(args.from_md) if args.from_md else (1, 1)
            to_md = _parse_cli_md(args.to_md) if args.to_md else (12, 31)
        except ValueError as exc:
            print(f"ERROR parsing period: {exc}", file=sys.stderr)
            sys.exit(1)
        years = None if args.year.lower() == "all" else [int(args.year)]
        try:
            resolution = select_period(meter, from_md, to_md, years)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        meter = resolution.meter
        resolution_notes = resolution.notes

    if meter.warnings:
        print("DATA WARNINGS:")
        for w in meter.warnings:
            print(f"  • {w}")
        print()

    engine = ComparisonEngine()
    result = engine.compare(meter, plans)

    for note in resolution_notes:
        print(f"  • {note}")
    if resolution_notes:
        print()

    days = result.period_days
    print(f"NMI: {result.nmi}  |  Analysis period: {days} days\n")
    print(f"{'Rank':<5} {'Plan':<35} {'Retailer':<20} {'Net Cost':>12} {'$/day':>8} {'Solar':>10}")
    print("-" * 95)
    for rank, entry in enumerate(result.ranked, start=1):
        daily = entry.baseline_net / days if days else Decimal("0")
        print(
            f"{rank:<5} {entry.plan_name:<35} {entry.retailer:<20} "
            f"${entry.baseline_net:>10.2f} ${daily:>6.2f} "
            f"-${entry.baseline_solar_credit:>8.2f}"
        )


if __name__ == "__main__":
    cli_main()
