"""Tab 3 — Results: ranked plan table, cost breakdown chart, delta report."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

import customtkinter as ctk

from power_analyser.core.comparison.report import ComparisonResult
from ..widgets.chart_widget import ChartWidget


class ResultsView(ctk.CTkFrame):
    """Tab 3 — ranked plan table, stacked bar chart, delta report."""

    def __init__(self, parent, **kwargs) -> None:
        super().__init__(parent, **kwargs)
        self._result: Optional[ComparisonResult] = None
        self._build_ui()

    # ── Layout ─────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # ── Header row with chart toggle ──
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, padx=16, pady=(12, 4), sticky="ew")
        header.columnconfigure(0, weight=1)

        ctk.CTkLabel(header, text="Results", font=ctk.CTkFont(size=15, weight="bold")).grid(
            row=0, column=0, sticky="w"
        )

        self._chart_mode_var = ctk.StringVar(value="breakdown")
        ctk.CTkSegmentedButton(
            header,
            values=["breakdown", "shift saving"],
            variable=self._chart_mode_var,
            command=self._refresh_chart,
        ).grid(row=0, column=1, padx=8)

        ctk.CTkButton(header, text="Export CSV…", width=100, command=self._export_csv).grid(
            row=0, column=2
        )

        # ── Main content: table (left) + chart (right) ──
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.grid(row=1, column=0, padx=16, pady=4, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=2)
        content.rowconfigure(0, weight=1)

        # Plan table
        table_frame = ctk.CTkFrame(content)
        table_frame.grid(row=0, column=0, padx=(0, 8), sticky="nsew")
        table_frame.rowconfigure(1, weight=1)

        self._table_header = ctk.CTkLabel(
            table_frame,
            text="Rank | Plan                  | Net $  | $/day | Solar",
            font=ctk.CTkFont(family="Courier", size=11),
            justify="left",
            anchor="w",
        )
        self._table_header.grid(row=0, column=0, padx=8, pady=(8, 0), sticky="ew")

        self._table_box = ctk.CTkTextbox(table_frame, font=ctk.CTkFont(family="Courier", size=11), state="disabled")
        self._table_box.grid(row=1, column=0, padx=8, pady=8, sticky="nsew")

        # Chart
        self._chart = ChartWidget(content)
        self._chart.grid(row=0, column=1, sticky="nsew")

        # ── Delta report ──
        ctk.CTkLabel(self, text="Load-Shift Delta (top 3 plans)", font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=2, column=0, padx=16, pady=(8, 0), sticky="w"
        )
        self._delta_box = ctk.CTkTextbox(
            self, font=ctk.CTkFont(family="Courier", size=11), height=100, state="disabled"
        )
        self._delta_box.grid(row=3, column=0, padx=16, pady=(4, 16), sticky="ew")

        # ── Warnings ──
        self._warn_label = ctk.CTkLabel(self, text="", text_color="#c44e52", anchor="w", wraplength=700)
        self._warn_label.grid(row=4, column=0, padx=16, pady=(0, 8), sticky="ew")

    # ── Public API ─────────────────────────────────────────────────────────────

    def show_result(self, result: ComparisonResult) -> None:
        """Populate the view with a new ComparisonResult."""
        self._result = result
        self._refresh_table()
        self._refresh_chart(self._chart_mode_var.get())
        self._refresh_delta()
        self._refresh_warnings()

    # ── Render helpers ─────────────────────────────────────────────────────────

    def _refresh_table(self) -> None:
        if not self._result:
            return
        days = max(self._result.period_days, 1)
        lines: list[str] = []
        for rank, entry in enumerate(self._result.ranked, start=1):
            daily = entry.baseline_net / days
            shift = f"-${entry.shift_saving:.2f}" if entry.shift_saving is not None else "    —  "
            lines.append(
                f"{rank:<4} {entry.plan_name[:22]:<22} ${entry.baseline_net:>8.2f} "
                f"${float(daily):>5.2f}  -{float(entry.baseline_solar_credit):>6.2f}  {shift}"
            )

        self._table_box.configure(state="normal")
        self._table_box.delete("1.0", "end")
        self._table_box.insert("end", "\n".join(lines))
        self._table_box.configure(state="disabled")

    def _refresh_chart(self, mode: str = "breakdown") -> None:
        if not self._result:
            return
        if mode == "shift saving":
            self._chart.plot_delta(self._result)
        else:
            self._chart.plot_breakdown(self._result)

    def _refresh_delta(self) -> None:
        if not self._result:
            return

        entries_with_shift = [
            e for e in self._result.ranked if e.shift_saving is not None
        ][:3]

        if not entries_with_shift:
            self._delta_box.configure(state="normal")
            self._delta_box.delete("1.0", "end")
            self._delta_box.insert("end", "No load-shift simulation was run.")
            self._delta_box.configure(state="disabled")
            return

        header = f"{'Plan':<30} {'Baseline':>10} {'Simulated':>10} {'Saving':>10}\n"
        separator = "-" * 64 + "\n"
        lines = [header, separator]
        for e in entries_with_shift:
            lines.append(
                f"{e.plan_name[:30]:<30} ${e.baseline_net:>9.2f} ${e.simulated_net:>9.2f} "
                f"${e.shift_saving:>9.2f}\n"
            )

        self._delta_box.configure(state="normal")
        self._delta_box.delete("1.0", "end")
        for line in lines:
            self._delta_box.insert("end", line)
        self._delta_box.configure(state="disabled")

    def _refresh_warnings(self) -> None:
        if not self._result or not self._result.warnings:
            self._warn_label.configure(text="")
            return
        text = "Warnings: " + "; ".join(self._result.warnings)
        self._warn_label.configure(text=text)

    # ── CSV export ─────────────────────────────────────────────────────────────

    def _export_csv(self) -> None:
        if not self._result:
            return
        from tkinter import filedialog
        import csv

        path = filedialog.asksaveasfilename(
            title="Export results as CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
        )
        if not path:
            return

        days = max(self._result.period_days, 1)
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Rank", "Plan ID", "Retailer", "Plan Name",
                "Supply $", "Usage $", "Solar Credit $", "Net $", "$/day",
                "Simulated Net $", "Shift Saving $", "Last Updated", "Conditions",
            ])
            for rank, e in enumerate(self._result.ranked, start=1):
                daily = e.baseline_net / days
                writer.writerow([
                    rank, e.plan_id, e.retailer, e.plan_name,
                    f"{e.baseline_supply:.4f}",
                    f"{e.baseline_usage:.4f}",
                    f"{e.baseline_solar_credit:.4f}",
                    f"{e.baseline_net:.4f}",
                    f"{float(daily):.4f}",
                    f"{e.simulated_net:.4f}" if e.simulated_net is not None else "",
                    f"{e.shift_saving:.4f}" if e.shift_saving is not None else "",
                    e.last_updated or "",
                    "; ".join(e.conditions),
                ])
