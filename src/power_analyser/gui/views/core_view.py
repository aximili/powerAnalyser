"""Tab 1 — Analyse: load NEM12, manage plans, configure load shift, run comparison.

Adds an **Analysis Period** selector: after a NEM12 file is picked it is parsed
in the background (no double-parse at Run). The user can analyse **All
available** data or a **Custom** day/month window. When the file spans multiple
years, matching calendar days are averaged so a seasonal pick uses every year.
"""

from __future__ import annotations

import datetime
import threading
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Callable, Optional

import customtkinter as ctk

from power_analyser.core.comparison.report import ComparisonEngine, ComparisonResult
from power_analyser.core.ingestion.pipeline import IngestionPipeline, MeterDataSet
from power_analyser.core.ingestion.period import (
    MonthDay,
    PeriodResolution,
    available_month_days,
    build_clamp_message,
    has_overlap,
    select_period,
    target_calendar_dates,
    years_overlapping_window,
)
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


def _fmt_date(d: datetime.date) -> str:
    """Full ``dd/mm/yyyy`` for the available-period label."""
    return f"{d.day}/{d.month}/{d.year}"


def _fmt_md(md: MonthDay) -> str:
    """``dd/mm`` from a ``(month, day)`` tuple."""
    return f"{md[1]}/{md[0]}"


def _parse_md(text: str) -> MonthDay:
    """Parse a ``dd/mm`` (or ``dd/mm/yyyy``) string into a ``(month, day)`` tuple.

    Raises ``ValueError`` on anything that isn't a real day/month pair.
    """
    parts = text.strip().split("/")
    if len(parts) < 2:
        raise ValueError("Period must be day/month, e.g. 1/6")
    try:
        day = int(parts[0])
        month = int(parts[1])
    except ValueError as exc:
        raise ValueError("Period must be day/month, e.g. 1/6") from exc
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise ValueError("Day/month out of range")
    # Reject impossible dates like 31/2 — uses a leap reference year so Feb 29 ok
    try:
        datetime.date(2000, month, day)
    except ValueError as exc:
        raise ValueError(f"{day}/{month} is not a valid date") from exc
    return (month, day)


class CoreView(ctk.CTkFrame):
    """Tab 1 — file inputs, analysis-period selector, plan list, Run button."""

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
        self._meter: Optional[MeterDataSet] = None

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

        # ── Analysis Period selector ──
        period_frame = ctk.CTkFrame(self)
        period_frame.grid(row=1, column=0, padx=16, pady=8, sticky="ew")
        period_frame.columnconfigure(1, weight=1)

        ctk.CTkLabel(
            period_frame,
            text="Analysis Period",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, columnspan=2, padx=8, pady=(8, 4), sticky="w")

        ctk.CTkLabel(period_frame, text="Available period:").grid(
            row=1, column=0, padx=8, pady=4, sticky="w"
        )
        self._available_label = ctk.CTkLabel(period_frame, text="—", anchor="w")
        self._available_label.grid(row=1, column=1, padx=8, pady=4, sticky="ew")

        # Segmented control on its own row (left), From/To in an inner frame (right)
        self._period_mode_var = ctk.StringVar(value="All available")
        seg = ctk.CTkSegmentedButton(
            period_frame,
            values=["All available", "Custom"],
            variable=self._period_mode_var,
            command=self._on_period_mode_change,
        )
        seg.grid(row=2, column=0, padx=8, pady=4, sticky="w")

        ft_frame = ctk.CTkFrame(period_frame, fg_color="transparent")
        ft_frame.grid(row=2, column=1, padx=8, pady=4, sticky="w")
        ctk.CTkLabel(ft_frame, text="From:").pack(side="left", padx=(0, 4))
        self._from_var = ctk.StringVar()
        self._from_entry = ctk.CTkEntry(ft_frame, width=70, textvariable=self._from_var, placeholder_text="dd/mm")
        self._from_entry.pack(side="left", padx=(0, 12))
        ctk.CTkLabel(ft_frame, text="To:").pack(side="left", padx=(0, 4))
        self._to_var = ctk.StringVar()
        self._to_entry = ctk.CTkEntry(ft_frame, width=70, textvariable=self._to_var, placeholder_text="dd/mm")
        self._to_entry.pack(side="left")

        ctk.CTkLabel(
            period_frame,
            text="Day/month. When your file spans multiple years, matching months are averaged (choose years at run time).",
            text_color="gray",
        ).grid(row=3, column=0, columnspan=2, padx=8, pady=(0, 8), sticky="w")

        # ── Plans section ──
        plans_frame = ctk.CTkFrame(self)
        plans_frame.grid(row=2, column=0, padx=16, pady=8, sticky="ew")
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
        ls_frame.grid(row=3, column=0, padx=16, pady=8, sticky="ew")
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
        bottom.grid(row=4, column=0, padx=16, pady=16, sticky="ew")
        bottom.columnconfigure(0, weight=1)

        self._status_label = ctk.CTkLabel(bottom, text="Ready", anchor="w")
        self._status_label.grid(row=0, column=0, sticky="ew")

        self._run_btn = ctk.CTkButton(bottom, text="Run Analysis", command=self._run_analysis)
        self._run_btn.grid(row=0, column=1, padx=(8, 0))
        # Run needs a parsed meter — disabled until one is loaded.
        self._run_btn.configure(state="disabled")
        self._on_period_mode_change()

    # ── File pickers ───────────────────────────────────────────────────────────

    def _set_nem12_path(self, path: Path) -> bool:
        """Adopt *path* as the current NEM12 file and parse it in the background.

        Returns ``True`` when the path was accepted.
        """
        if not path.exists() or not path.is_file():
            messagebox.showerror("File not found", f"Could not find:\n{path}")
            return False
        self._nem12_path = path
        self._nem12_entry.delete(0, "end")
        self._nem12_entry.insert(0, str(path))
        self._begin_parse(path)
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

    # ── Background NEM12 parse ─────────────────────────────────────────────────

    def _begin_parse(self, path: Path) -> None:
        """Parse the NEM12 in a daemon thread; update state on the main thread."""
        self._meter = None
        self._available_label.configure(text="—")
        self._run_btn.configure(state="disabled", text="Loading…")
        self._set_status(f"Loading NEM12: {path.name}…")
        thread = threading.Thread(target=self._parse_meter, args=(path,), daemon=True)
        thread.start()

    def _parse_meter(self, path: Path) -> None:
        try:
            meter = IngestionPipeline().load(path)
            self.after(0, lambda m=meter: self._on_meter_loaded(m))
        except Exception as exc:
            import sys
            import traceback

            tb = traceback.format_exc()
            # Print the full traceback to the console (gui.bat terminal) so it
            # is always available even if the dialog is truncated/closed.
            print(f"NEM12 parse failed for {path}:\n{tb}", file=sys.stderr)
            self.after(0, lambda e=exc, t=tb: self._on_parse_error(e, t, path))

    def _on_meter_loaded(self, meter: MeterDataSet) -> None:
        self._meter = meter
        start, end = meter.start_date, meter.end_date
        self._available_label.configure(text=f"{_fmt_date(start)} – {_fmt_date(end)}")
        # Default the Custom window to the full available range (dd/mm).
        self._from_var.set(f"{start.day}/{start.month}")
        self._to_var.set(f"{end.day}/{end.month}")
        self._run_btn.configure(state="normal", text="Run Analysis")
        n_days = len(set(meter.e1.index.date))
        self._set_status(
            f"Loaded NEM12: NMI {meter.nmi} — {n_days} days, "
            f"{_fmt_date(start)}–{_fmt_date(end)}"
        )

    def _on_parse_error(
        self, exc: Exception, tb: str = "", path: Path | None = None
    ) -> None:
        self._meter = None
        self._available_label.configure(text="—")
        self._run_btn.configure(state="disabled", text="Run Analysis")
        # Show the full traceback (type + message + frames) so the failure point
        # is visible, not just a bare message. Truncate for the dialog; the
        # complete traceback was already printed to the console.
        detail = tb or f"{type(exc).__name__}: {exc}"
        if len(detail) > 2000:
            detail = detail[:2000] + "\n…(truncated — full traceback in console)"
        where = f" ({path.name})" if path else ""
        self._set_status(f"Error loading NEM12{where}: {exc}")
        messagebox.showerror("NEM12 load failed", detail)


    # ── Period selector ────────────────────────────────────────────────────────

    def _on_period_mode_change(self, _value: str | None = None) -> None:
        custom = self._period_mode_var.get() == "Custom"
        state = "normal" if custom else "disabled"
        self._from_entry.configure(state=state)
        self._to_entry.configure(state=state)

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
        """Restore NEM12 path + period settings; kick off a parse if path exists."""
        stored = self._settings.get("nem12_path", "")
        if stored:
            path = Path(stored)
            if path.exists() and path.is_file():
                self._nem12_path = path
                self._nem12_entry.insert(0, str(path))
                self._begin_parse(path)

        mode = self._settings.get("period_mode", "all")
        self._period_mode_var.set("Custom" if mode == "custom" else "All available")
        self._from_var.set(self._settings.get("period_from", "") or "")
        self._to_var.set(self._settings.get("period_to", "") or "")
        self._on_period_mode_change()

    def collect_state(self, settings: dict[str, Any]) -> None:
        """Write current inputs into *settings* for persistence."""
        settings["nem12_path"] = str(self._nem12_path) if self._nem12_path else ""
        settings["period_mode"] = "custom" if self._period_mode_var.get() == "Custom" else "all"
        settings["period_from"] = self._from_var.get()
        settings["period_to"] = self._to_var.get()

    # ── Add plans injected from agent ─────────────────────────────────────────

    def add_plans_from_agent(self, plans: list[ElectricityPlan]) -> None:
        """Called by the GUI when the agent finds new plans."""
        self._plans.extend(plans)
        self._refresh_plan_list()
        self._set_status(f"Agent added {len(plans)} plan(s). Total: {len(self._plans)}.")

    # ── Analysis ───────────────────────────────────────────────────────────────

    def _run_analysis(self) -> None:
        if self._meter is None:
            messagebox.showwarning("Missing input", "Please select a valid NEM12 file first.")
            return
        if not self._plans:
            messagebox.showwarning("Missing input", "Please add at least one electricity plan.")
            return

        # Resolve the analysis window + (optional) years.
        if self._period_mode_var.get() == "All available":
            from_md: MonthDay = (1, 1)
            to_md: MonthDay = (12, 31)
            years: Optional[list[int]] = None
        else:
            try:
                from_md = _parse_md(self._from_var.get())
                to_md = _parse_md(self._to_var.get())
            except ValueError as exc:
                messagebox.showerror("Invalid period", str(exc))
                return
            years = None
            overlapping = years_overlapping_window(self._meter, from_md, to_md)
            if len(overlapping) >= 2:
                dlg = _YearChooserDialog(self, overlapping)
                self.wait_window(dlg)
                if dlg.cancelled:
                    return
                years = dlg.years  # None ⇒ all (averaged)

        # Clamp / overlap validation (calendar-window level).
        window = target_calendar_dates(from_md, to_md)
        avail = available_month_days(self._meter, years)
        if not has_overlap(window, avail):
            if avail:
                avail_sorted = sorted(avail)
                messagebox.showerror(
                    "No data in period",
                    f"No data in the selected period. "
                    f"Available: {_fmt_md(avail_sorted[0])}–{_fmt_md(avail_sorted[-1])}.",
                )
            else:
                messagebox.showerror("No data in period", "No data in the selected period.")
            return
        clamp_msg = build_clamp_message(window, avail)
        if clamp_msg and not messagebox.askyesno("Trim period", clamp_msg):
            return

        self._run_btn.configure(state="disabled", text="Running…")
        self._set_status("Analysing…")
        thread = threading.Thread(
            target=self._run_in_background,
            args=(from_md, to_md, years),
            daemon=True,
        )
        thread.start()

    def _run_in_background(
        self, from_md: MonthDay, to_md: MonthDay, years: Optional[list[int]]
    ) -> None:
        try:
            resolution: PeriodResolution = select_period(self._meter, from_md, to_md, years)

            elasticity_configs = self._build_elasticity_configs()

            engine = ComparisonEngine()
            result = engine.compare(resolution.meter, self._plans, elasticity_configs or None)

            self.after(0, lambda: self._on_done(result, resolution))
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

    def _on_done(self, result, resolution: Optional[PeriodResolution] = None) -> None:
        self._run_btn.configure(state="normal", text="Run Analysis")
        days = result.period_days
        note = ""
        if resolution and resolution.notes:
            note = " " + " ".join(resolution.notes)
        self._set_status(
            f"Done. {len(result.ranked)} plans compared over {days} days. "
            f"Best: {result.ranked[0].plan_name} — ${result.ranked[0].baseline_net:.2f}{note}"
        )
        self._on_result(result)

    def _on_error(self, exc: Exception) -> None:
        self._run_btn.configure(state="normal", text="Run Analysis")
        self._set_status(f"Error: {exc}")
        messagebox.showerror("Analysis failed", str(exc))

    def _set_status(self, text: str) -> None:
        self._status_label.configure(text=text)


class _YearChooserDialog(ctk.CTkToplevel):
    """Modal dialog asking which years to use when a window spans ≥2 years.

    ``years`` is ``None`` for "Both (averaged)" (all years), or a single-element
    list ``[YYYY]``. ``cancelled`` is ``True`` if the user closed/cancelled.
    """

    def __init__(self, parent, years: list[int]) -> None:
        super().__init__(parent)
        self.title("Multiple years available")
        self.transient(parent)
        self.resizable(False, False)

        self.years: Optional[list[int]] = None
        self.cancelled = True

        options = ["Both (averaged)"] + [str(y) for y in years]
        self._var = ctk.StringVar(value="Both (averaged)")

        ctk.CTkLabel(
            self,
            text="Your period spans multiple years.\nChoose which to use:",
        ).grid(row=0, column=0, columnspan=2, padx=16, pady=(16, 8), sticky="w")

        for idx, opt in enumerate(options):
            ctk.CTkRadioButton(
                self, text=opt, variable=self._var, value=opt
            ).grid(row=idx + 1, column=0, columnspan=2, padx=24, pady=2, sticky="w")

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=len(options) + 1, column=0, columnspan=2, padx=16, pady=(12, 16), sticky="ew")
        ctk.CTkButton(btns, text="Cancel", width=90, fg_color="#c44e52", command=self._cancel).pack(
            side="right", padx=(8, 0)
        )
        ctk.CTkButton(btns, text="OK", width=90, command=self._ok).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._cancel)

        try:
            self.grab_set()
        except Exception:
            pass

        # Centre over the parent
        self.update_idletasks()
        if parent.winfo_exists():
            px = parent.winfo_rootx() + 40
            py = parent.winfo_rooty() + 40
            self.geometry(f"+{px}+{py}")

    def _ok(self) -> None:
        choice = self._var.get()
        if choice == "Both (averaged)":
            self.years = None
        else:
            self.years = [int(choice)]
        self.cancelled = False
        self.destroy()

    def _cancel(self) -> None:
        self.cancelled = True
        self.years = None
        self.destroy()
