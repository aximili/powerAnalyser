"""Main GUI entry point — three-tab desktop application.

Launch with:
    python -m power_analyser.gui.app
or, after installation:
    power-analyser-gui
"""

from __future__ import annotations

import sys

import customtkinter as ctk

from .views.core_view import CoreView
from .views.agent_view import AgentView
from .views.results_view import ResultsView


class PowerAnalyserApp(ctk.CTk):
    """Main application window with three tabs: Analyse, Agent, Results."""

    def __init__(self) -> None:
        super().__init__()

        self.title("Power Analyser — Victorian Electricity Plan Comparison")
        self.geometry("960x720")
        self.minsize(800, 600)

        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")

        self._build_ui()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        notebook = ctk.CTkTabview(self)
        notebook.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

        notebook.add("Analyse")
        notebook.add("Agent")
        notebook.add("Results")

        # Results view is created first so CoreView can reference it via callback
        self._results_view = ResultsView(notebook.tab("Results"))
        self._results_view.pack(fill="both", expand=True)

        self._core_view = CoreView(
            notebook.tab("Analyse"),
            on_result=self._on_analysis_complete,
        )
        self._core_view.pack(fill="both", expand=True)

        self._agent_view = AgentView(
            notebook.tab("Agent"),
            on_plans_found=self._on_agent_plans,
        )
        self._agent_view.pack(fill="both", expand=True)

        # Keep a reference so we can switch to Results automatically
        self._notebook = notebook

    # ── Callbacks ──────────────────────────────────────────────────────────────

    def _on_analysis_complete(self, result) -> None:
        """Show results and switch to the Results tab."""
        self._results_view.show_result(result)
        self._notebook.set("Results")

    def _on_agent_plans(self, plans) -> None:
        """Forward extracted plans to the Analyse tab and notify user."""
        self._core_view.add_plans_from_agent(plans)
        self._notebook.set("Analyse")


def main() -> None:
    app = PowerAnalyserApp()
    app.mainloop()


if __name__ == "__main__":
    main()
