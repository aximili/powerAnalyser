"""Tab 1 — Analyse: load NEM12, manage plans, configure load shift, run comparison."""

from __future__ import annotations

import threading
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Callable, Optional

import customtkinter as ctk

from power_analyser.core.comparison.report import ComparisonEngine, ComparisonResult
from power_analyser.core.ingestion.pipeline import IngestionPipeline
from power_analyser.core.simulation.elasticity import (
    ElasticityConfig,
    LoadShiftSimulator,
    SourceWindow,
)
from power_analyser.core.tariff.loader import load_plan, load_plans_dir
from power_analyser.core.tariff.schema import ElectricityPlan


def _parse_dropped_files(data: str) -> list[str]:
    """Parse a tkdnd ``<<Drop>>`` payload into a list of file paths.

    tkdnd separates multiple files with spaces and wraps paths that contain
    spaces (or the platform's path with a drive letter) in ``{...}``.
    """
    files: list[str] = []
    i, n = 0, len(data)
    while i < n:
        if data[i] == "{":
            end = data.find("}", i)
            if end == -1:
                break
            files.append(data[i + 1 : end])
            i = end + 1
        else:
            end = data.find(" ", i)
            if end == -1:
                files.append(data[i:])
                break
            files.append(data[i:end])
            i = end + 1
    return [f.strip() for f in files if f.strip()]


class CoreView(ctk.CTkFrame):
    """Tab 1 — file inputs, plan list, load-shift configuration, Run button."""

    def __init__(
        self,
        parent,
        on_result: Callable[[ComparisonResult], None],
        settings: Optional[dict[str, Any]] = None,
        **kwargs,
    ) -> None:
        super().__init__(parent, **kwargs)
        self._on_result = on_result
        self._settings = settings or {}
        self._nem12_path: Optional[Path] = None
        self._dnd_active = False
        self._plans: list[ElectricityPlan] = []

        self._build_ui()
        self._apply_settings()
        self._setup_dnd()

    # ── Layout ─────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)

        # ── NEM12 file picker ──
        self._nem12_frame = ctk.CTkFrame(self)
        self._nem12_frame.grid(row=0, column=0, padx=16, pady=(16, 8), sticky="ew")
        self._nem12_frame.columnconfigure(1, weight=1)

        ctk.CTkLabel(self._nem12_frame, text="NEM12 file:").grid(row=0, column=0, padx=8, pady=8, sticky="w")
        self._nem12_entry = ctk.CTkEntry(
            self._nem12_frame, placeholder_text="Drag & drop a file here, or click Browse…"
        )
        self._nem12_entry.grid(row=0, column=1, padx=8, pady=8, sticky="ew")
        ctk.CTkButton(self._nem12_frame, text="Browse…", width=90, command=self._pick_nem12).grid(
            row=0, column=2, padx=8, pady=8
        )

        # ── Plans section ──
        plans_frame = ctk.CTkFrame(self)
        plans_frame.grid(row=1, column=0, padx=16, pady=8, sticky="ew")
        plans_frame.columnconfigure(0, weight=1)

        ctk.CTkLabel(plans_frame, text="Electricity Plans", font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=0, columnspan=3, padx=8, pady=(8, 4), sticky="w"
        )

        self._plan_listbox = ctk.CTkTextbox(plans_frame, height=120, state="disabled")
        self._plan_listbox.grid(row=1, column=0, padx=(8, 4), pady=4, sticky="ew")

        btn_col = ctk.CTkFrame(plans_frame, fg_color="transparent")
        btn_col.grid(row=1, column=1, padx=(0, 8), pady=4, sticky="ns")
        ctk.CTkButton(btn_col, text="Add JSON…", width=100, command=self._add_plan_file).pack(
            pady=(0, 4)
        )
        ctk.CTkButton(btn_col, text="Add Folder…", width=100, command=self._add_plans_dir).pack(
            pady=(0, 4)
        )
        ctk.CTkButton(btn_col, text="Clear All", width=100, fg_color="#c44e52", command=self._clear_plans).pack()

        # ── Load-shift config ──
        ls_frame = ctk.CTkFrame(self)
        ls_frame.grid(row=2, column=0, padx=16, pady=8, sticky="ew")
        ls_frame.columnconfigure((1, 3), weight=1)

        ctk.CTkLabel(ls_frame, text="Load-Shift (optional)", font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=0, columnspan=4, padx=8, pady=(8, 4), sticky="w"
        )

        ctk.CTkLabel(ls_frame, text="Source window name:").grid(row=1, column=0, padx=8, pady=4, sticky="w")
        self._source_window_entry = ctk.CTkEntry(ls_frame, placeholder_text="e.g. Peak")
        self._source_window_entry.grid(row=1, column=1, padx=8, pady=4, sticky="ew")

        ctk.CTkLabel(ls_frame, text="Target window name:").grid(row=1, column=2, padx=8, pady=4, sticky="w")
        self._target_window_entry = ctk.CTkEntry(ls_frame, placeholder_text="e.g. Midday Power Saver")
        self._target_window_entry.grid(row=1, column=3, padx=8, pady=4, sticky="ew")

        ctk.CTkLabel(ls_frame, text="Shift fraction (0–1):").grid(row=2, column=0, padx=8, pady=4, sticky="w")
        self._shift_fraction_entry = ctk.CTkEntry(ls_frame, placeholder_text="0.5")
        self._shift_fraction_entry.grid(row=2, column=1, padx=8, pady=4, sticky="ew")

        # ── Status + Run ──
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=3, column=0, padx=16, pady=16, sticky="ew")
        bottom.columnconfigure(0, weight=1)

        self._status_label = ctk.CTkLabel(bottom, text="Ready", anchor="w")
        self._status_label.grid(row=0, column=0, sticky="ew")

        self._run_btn = ctk.CTkButton(bottom, text="Run Analysis", command=self._run_analysis)
        self._run_btn.grid(row=0, column=1, padx=(8, 0))

    # ── File pickers ───────────────────────────────────────────────────────────

    def _set_nem12_path(self, path: Path) -> bool:
        """Adopt *path* as the current NEM12 file if it exists.

        Returns ``True`` when the path was accepted.
        """
        if not path.exists() or not path.is_file():
            messagebox.showerror("File not found", f"Could not find:\n{path}")
            return False
        self._nem12_path = path
        self._nem12_entry.delete(0, "end")
        self._nem12_entry.insert(0, str(path))
        self._set_status(f"NEM12: {path.name}")
        return True

    def _pick_nem12(self) -> None:
        path = filedialog.askopenfilename(
            title="Select NEM12 file",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if path:
            self._set_nem12_path(Path(path))

    def _add_plan_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Select plan JSON",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            plan = load_plan(Path(path))
            self._plans.append(plan)
            self._refresh_plan_list()
        except Exception as exc:
            messagebox.showerror("Invalid plan", str(exc))

    def _add_plans_dir(self) -> None:
        directory = filedialog.askdirectory(title="Select folder of plan JSON files")
        if not directory:
            return
        new_plans = load_plans_dir(Path(directory))
        if not new_plans:
            messagebox.showwarning("No plans found", "No valid plan JSON files found in that folder.")
            return
        self._plans.extend(new_plans)
        self._refresh_plan_list()
        self._set_status(f"Loaded {len(new_plans)} plan(s) from folder.")

    def _clear_plans(self) -> None:
        self._plans.clear()
        self._refresh_plan_list()

    def _refresh_plan_list(self) -> None:
        self._plan_listbox.configure(state="normal")
        self._plan_listbox.delete("1.0", "end")
        for plan in self._plans:
            self._plan_listbox.insert("end", f"• {plan.retailer} — {plan.plan_name}\n")
        self._plan_listbox.configure(state="disabled")

    # ── Drag & drop ────────────────────────────────────────────────────────────

    def _setup_dnd(self) -> None:
        """Register the NEM12 input box as a file drop target if tkdnd is present.

        tkdnd is optional — if it (or the native library) is unavailable the app
        simply falls back to the Browse button.
        """
        try:
            from tkinterdnd2 import DND_FILES
        except ImportError:
            return
        try:
            self._nem12_frame.drop_target_register(DND_FILES)
            self._nem12_frame.dnd_bind("<<Drop>>", self._on_nem12_drop)
            self._nem12_entry.drop_target_register(DND_FILES)
            self._nem12_entry.dnd_bind("<<Drop>>", self._on_nem12_drop)
            self._dnd_active = True
        except Exception:
            self._dnd_active = False

    def _on_nem12_drop(self, event) -> None:
        files = _parse_dropped_files(getattr(event, "data", ""))
        if files:
            self._set_nem12_path(Path(files[0]))

    # ── Settings persistence ───────────────────────────────────────────────────

    def _apply_settings(self) -> None:
        """Restore the previously-chosen NEM12 path if it still exists."""
        stored = self._settings.get("nem12_path", "")
        if stored:
            path = Path(stored)
            if path.exists() and path.is_file():
                self._nem12_path = path
                self._nem12_entry.insert(0, str(path))

    def collect_state(self, settings: dict[str, Any]) -> None:
        """Write the current NEM12 path into *settings* for persistence."""
        settings["nem12_path"] = str(self._nem12_path) if self._nem12_path else ""

    # ── Add plans injected from agent ─────────────────────────────────────────

    def add_plans_from_agent(self, plans: list[ElectricityPlan]) -> None:
        """Called by the GUI when the agent finds new plans."""
        self._plans.extend(plans)
        self._refresh_plan_list()
        self._set_status(f"Agent added {len(plans)} plan(s). Total: {len(self._plans)}.")

    # ── Analysis ───────────────────────────────────────────────────────────────

    def _run_analysis(self) -> None:
        if not self._nem12_path:
            messagebox.showwarning("Missing input", "Please select a NEM12 file first.")
            return
        if not self._plans:
            messagebox.showwarning("Missing input", "Please add at least one electricity plan.")
            return

        self._run_btn.configure(state="disabled", text="Running…")
        self._set_status("Loading meter data…")

        thread = threading.Thread(target=self._run_in_background, daemon=True)
        thread.start()

    def _run_in_background(self) -> None:
        try:
            pipeline = IngestionPipeline()
            meter = pipeline.load(self._nem12_path)

            elasticity_configs = self._build_elasticity_configs()

            engine = ComparisonEngine()
            result = engine.compare(meter, self._plans, elasticity_configs or None)

            self.after(0, lambda: self._on_done(result))
        except Exception as exc:
            self.after(0, lambda e=exc: self._on_error(e))

    def _build_elasticity_configs(self) -> dict:
        source_name = self._source_window_entry.get().strip()
        target_name = self._target_window_entry.get().strip()
        fraction_text = self._shift_fraction_entry.get().strip()

        if not source_name or not target_name:
            return {}

        try:
            fraction = float(fraction_text) if fraction_text else 0.5
        except ValueError:
            fraction = 0.5

        configs = {}
        for plan in self._plans:
            # Only add config if the plan has a matching target free window
            has_target = any(fw.name == target_name for fw in plan.free_windows)
            if not has_target:
                continue
            # Find the tier whose schedule defines the source window (by tier name)
            source_tier = next(
                (t for t in plan.usage_tiers if t.name == source_name), None
            )
            if not source_tier:
                continue
            sw = SourceWindow(schedule=source_tier.schedule, shift_fraction=fraction)
            configs[plan.plan_id] = ElasticityConfig(
                source_windows=[sw], target_window_name=target_name
            )
        return configs

    def _on_done(self, result) -> None:
        self._run_btn.configure(state="normal", text="Run Analysis")
        days = result.period_days
        self._set_status(
            f"Done. {len(result.ranked)} plans compared over {days} days. "
            f"Best: {result.ranked[0].plan_name} — ${result.ranked[0].baseline_net:.2f}"
        )
        self._on_result(result)

    def _on_error(self, exc: Exception) -> None:
        self._run_btn.configure(state="normal", text="Run Analysis")
        self._set_status(f"Error: {exc}")
        messagebox.showerror("Analysis failed", str(exc))

    def _set_status(self, text: str) -> None:
        self._status_label.configure(text=text)
