"""Reusable matplotlib chart embedded in a CustomTkinter frame."""

from __future__ import annotations

from typing import Optional

import customtkinter as ctk

try:
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
    import matplotlib.pyplot as plt
    _MATPLOTLIB_AVAILABLE = True
except ImportError:
    _MATPLOTLIB_AVAILABLE = False


class ChartWidget(ctk.CTkFrame):
    """A CustomTkinter frame containing an embedded matplotlib figure.

    Call ``plot_breakdown(comparison_result)`` to render the cost breakdown
    bar chart, or ``clear()`` to remove the current chart.
    """

    def __init__(self, parent, width: int = 700, height: int = 320, **kwargs):
        super().__init__(parent, **kwargs)

        if not _MATPLOTLIB_AVAILABLE:
            ctk.CTkLabel(self, text="matplotlib not installed — charts unavailable").pack(pady=20)
            return

        self._figure = Figure(figsize=(width / 100, height / 100), dpi=100, tight_layout=True)
        self._canvas = FigureCanvasTkAgg(self._figure, master=self)
        self._canvas.get_tk_widget().pack(fill="both", expand=True)

    def clear(self) -> None:
        if not _MATPLOTLIB_AVAILABLE:
            return
        self._figure.clear()
        self._canvas.draw()

    def plot_breakdown(self, comparison_result) -> None:
        """Render a stacked bar chart of supply / usage / solar for each plan.

        ``comparison_result`` is a ``ComparisonResult`` from the comparison engine.
        """
        if not _MATPLOTLIB_AVAILABLE:
            return
        if not comparison_result or not comparison_result.ranked:
            return

        self._figure.clear()
        ax = self._figure.add_subplot(111)

        entries = comparison_result.ranked
        names = [f"{e.retailer}\n{e.plan_name}" for e in entries]
        supply = [float(e.baseline_supply) for e in entries]
        usage = [float(e.baseline_usage) for e in entries]
        solar = [-float(e.baseline_solar_credit) for e in entries]  # negative = credit

        x = range(len(names))
        bar_w = 0.5

        bars_supply = ax.bar(x, supply, bar_w, label="Supply charge", color="#4c72b0")
        bars_usage = ax.bar(x, usage, bar_w, bottom=supply, label="Usage cost", color="#dd8452")
        bars_solar = ax.bar(x, solar, bar_w, label="Solar credit (−)", color="#55a868")

        ax.set_xticks(list(x))
        ax.set_xticklabels(names, fontsize=8)
        ax.set_ylabel("$ over period")
        ax.set_title("Plan Cost Breakdown")
        ax.legend(fontsize=8)
        ax.axhline(0, color="black", linewidth=0.5)

        self._canvas.draw()

    def plot_delta(self, comparison_result) -> None:
        """Render a horizontal bar chart of load-shift savings per plan."""
        if not _MATPLOTLIB_AVAILABLE:
            return

        entries = [e for e in comparison_result.ranked if e.shift_saving is not None]
        if not entries:
            return

        self._figure.clear()
        ax = self._figure.add_subplot(111)

        names = [f"{e.retailer}\n{e.plan_name}" for e in entries]
        savings = [float(e.shift_saving) for e in entries]

        colours = ["#55a868" if s >= 0 else "#c44e52" for s in savings]
        ax.barh(range(len(names)), savings, color=colours)
        ax.set_yticks(range(len(names)))
        ax.set_yticklabels(names, fontsize=8)
        ax.set_xlabel("$ saving from load shift (positive = cheaper)")
        ax.set_title("Load-Shift Savings vs Baseline")
        ax.axvline(0, color="black", linewidth=0.5)

        self._canvas.draw()
