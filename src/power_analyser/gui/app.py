"""Main GUI entry point — three-tab desktop application.

Launch with:
    python -m power_analyser.gui.app
or, after installation:
    power-analyser-gui
"""

from __future__ import annotations

import customtkinter as ctk

from .settings import load_settings, save_settings
from .views.core_view import CoreView
from .views.agent_view import AgentView
from .views.manual_view import ManualView
from .views.results_view import ResultsView
from .widgets.llm_config_frame import default_llm_vars


class PowerAnalyserApp(ctk.CTk):
    """Main application window with tabs: Analyse, Agent, Manual, Results."""

    def __init__(self) -> None:
        super().__init__()

        self.title("Power Analyser — Victorian Electricity Plan Comparison")
        self.geometry("1000x760")
        self.minsize(860, 640)

        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")

        self._settings: dict = load_settings()
        # Shared by the Agent and Manual tabs so the LLM config stays in sync.
        self._llm_vars = default_llm_vars()

        # Persist settings when the user closes the window.
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        # Enable native drag & drop on this root window (loads the tkdnd
        # library).  Optional — if it fails the app still works via Browse.
        self._init_dnd()

        notebook = ctk.CTkTabview(self)
        notebook.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

        notebook.add("Analyse")
        notebook.add("Agent")
        notebook.add("Manual")
        notebook.add("Results")

        # Results view is created first so CoreView can reference it via callback
        self._results_view = ResultsView(notebook.tab("Results"))
        self._results_view.pack(fill="both", expand=True)

        self._core_view = CoreView(
            notebook.tab("Analyse"),
            on_result=self._on_analysis_complete,
            settings=self._settings,
        )
        self._core_view.pack(fill="both", expand=True)

        self._agent_view = AgentView(
            notebook.tab("Agent"),
            on_plans_found=self._on_agent_plans,
            settings=self._settings,
            llm_vars=self._llm_vars,
        )
        self._agent_view.pack(fill="both", expand=True)

        self._manual_view = ManualView(
            notebook.tab("Manual"),
            on_plans_found=self._on_agent_plans,
            settings=self._settings,
            llm_vars=self._llm_vars,
        )
        self._manual_view.pack(fill="both", expand=True)

        # Keep a reference so we can switch to Results automatically
        self._notebook = notebook

    # ── Drag & drop ────────────────────────────────────────────────────────────

    def _init_dnd(self) -> None:
        """Load the tkdnd native library on this window to enable file DnD."""
        try:
            import tkinterdnd2

            tkinterdnd2.TkinterDnD._require(self)
            self._dnd_enabled = True
        except Exception:
            self._dnd_enabled = False

    # ── Callbacks ──────────────────────────────────────────────────────────────

    def _on_analysis_complete(self, result) -> None:
        """Show results and switch to the Results tab."""
        self._results_view.show_result(result)
        self._notebook.set("Results")

    def _on_agent_plans(self, plans) -> None:
        """Forward extracted plans to the Analyse tab and notify user."""
        self._core_view.add_plans_from_agent(plans)
        self._notebook.set("Analyse")

    def _on_close(self) -> None:
        """Gather current field values from each tab and persist them."""
        self._core_view.collect_state(self._settings)
        self._agent_view.collect_state(self._settings)
        self._manual_view.collect_state(self._settings)
        save_settings(self._settings)
        self.destroy()


def main() -> None:
    app = PowerAnalyserApp()
    app.mainloop()


if __name__ == "__main__":
    main()
