"""Tab 2 — Agent: configure LLM provider, target URL, start/stop, CAPTCHA handling."""

from __future__ import annotations

import threading
from typing import Callable, Optional

import customtkinter as ctk

from power_analyser.config import Config, get_config
from power_analyser.core.tariff.schema import ElectricityPlan


class AgentView(ctk.CTkFrame):
    """Tab 2 — LLM config, target URL, run/stop, live log, CAPTCHA banner."""

    def __init__(
        self,
        parent,
        on_plans_found: Callable[[list[ElectricityPlan]], None],
        **kwargs,
    ) -> None:
        super().__init__(parent, **kwargs)
        self._on_plans_found = on_plans_found
        self._orchestrator = None
        self._agent_thread: Optional[threading.Thread] = None
        self._running = False

        self._build_ui()

    # ── Layout ─────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)

        # ── LLM provider config ──
        llm_frame = ctk.CTkFrame(self)
        llm_frame.grid(row=0, column=0, padx=16, pady=(16, 8), sticky="ew")
        llm_frame.columnconfigure((1, 3), weight=1)

        ctk.CTkLabel(llm_frame, text="LLM Configuration", font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=0, columnspan=4, padx=8, pady=(8, 4), sticky="w"
        )

        ctk.CTkLabel(llm_frame, text="Provider:").grid(row=1, column=0, padx=8, pady=4, sticky="w")
        self._provider_var = ctk.StringVar(value="ollama")
        provider_menu = ctk.CTkOptionMenu(
            llm_frame,
            variable=self._provider_var,
            values=["ollama", "glm", "openai"],
            command=self._on_provider_change,
        )
        provider_menu.grid(row=1, column=1, padx=8, pady=4, sticky="ew")

        ctk.CTkLabel(llm_frame, text="Model:").grid(row=1, column=2, padx=8, pady=4, sticky="w")
        self._model_entry = ctk.CTkEntry(llm_frame, placeholder_text="e.g. llama3.2")
        self._model_entry.grid(row=1, column=3, padx=8, pady=4, sticky="ew")

        ctk.CTkLabel(llm_frame, text="API Key / Base URL:").grid(row=2, column=0, padx=8, pady=4, sticky="w")
        self._api_key_entry = ctk.CTkEntry(llm_frame, placeholder_text="API key or base URL", show="")
        self._api_key_entry.grid(row=2, column=1, columnspan=3, padx=8, pady=4, sticky="ew")

        # ── Task / URL ──
        url_frame = ctk.CTkFrame(self)
        url_frame.grid(row=1, column=0, padx=16, pady=8, sticky="ew")
        url_frame.columnconfigure(1, weight=1)

        ctk.CTkLabel(url_frame, text="Target URL:").grid(row=0, column=0, padx=8, pady=4, sticky="w")
        self._url_entry = ctk.CTkEntry(url_frame, placeholder_text="https://retailer.com/plans")
        self._url_entry.grid(row=0, column=1, padx=8, pady=4, sticky="ew")

        ctk.CTkLabel(url_frame, text="Task prompt:").grid(row=1, column=0, padx=8, pady=4, sticky="w")
        self._task_entry = ctk.CTkEntry(
            url_frame,
            placeholder_text="Find all residential electricity plans and extract pricing",
        )
        self._task_entry.grid(row=1, column=1, padx=8, pady=4, sticky="ew")

        # ── Control buttons ──
        ctrl_frame = ctk.CTkFrame(self, fg_color="transparent")
        ctrl_frame.grid(row=2, column=0, padx=16, pady=4, sticky="ew")

        self._start_btn = ctk.CTkButton(ctrl_frame, text="Start Agent", command=self._start_agent)
        self._start_btn.pack(side="left", padx=(0, 8))

        self._stop_btn = ctk.CTkButton(
            ctrl_frame, text="Stop", fg_color="#c44e52", command=self._stop_agent, state="disabled"
        )
        self._stop_btn.pack(side="left")

        # ── CAPTCHA banner (hidden by default) ──
        self._captcha_frame = ctk.CTkFrame(self, fg_color="#8B4513")
        # Not gridded until a CAPTCHA is detected

        ctk.CTkLabel(
            self._captcha_frame,
            text="CAPTCHA detected! Please solve it in the browser window, then click Resume.",
            text_color="white",
        ).pack(side="left", padx=12, pady=8, expand=True, fill="x")

        ctk.CTkButton(
            self._captcha_frame,
            text="Resume",
            fg_color="#55a868",
            command=self._resume_after_captcha,
        ).pack(side="right", padx=8, pady=8)

        # ── Live log ──
        ctk.CTkLabel(self, text="Agent Log", font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=4, column=0, padx=16, pady=(8, 0), sticky="w"
        )
        self._log_box = ctk.CTkTextbox(self, height=220, state="disabled")
        self._log_box.grid(row=5, column=0, padx=16, pady=(4, 16), sticky="nsew")
        self.rowconfigure(5, weight=1)

    # ── Provider change ────────────────────────────────────────────────────────

    def _on_provider_change(self, value: str) -> None:
        placeholders = {
            "ollama": "http://localhost:11434",
            "glm": "Zhipu AI API key",
            "openai": "OpenAI API key (or leave blank for env var)",
        }
        self._api_key_entry.configure(placeholder_text=placeholders.get(value, ""))

    # ── Agent lifecycle ────────────────────────────────────────────────────────

    def _start_agent(self) -> None:
        url = self._url_entry.get().strip()
        if not url:
            self._log("ERROR: No target URL set.")
            return

        task = self._task_entry.get().strip() or (
            "Find all residential electricity plans and extract pricing details"
        )

        try:
            config = self._build_config()
        except Exception as exc:
            self._log(f"ERROR building config: {exc}")
            return

        # Import here so core tests don't need agent deps
        from power_analyser.agent.llm.base import create_provider
        from power_analyser.agent.orchestrator import AgentOrchestrator

        try:
            provider = create_provider(config)
        except Exception as exc:
            self._log(f"ERROR creating LLM provider: {exc}")
            return

        self._orchestrator = AgentOrchestrator(provider, config)
        self._running = True
        self._start_btn.configure(state="disabled")
        self._stop_btn.configure(state="normal")
        self._log(f"Starting agent → {url}")

        self._agent_thread = threading.Thread(
            target=self._run_agent, args=(task, url), daemon=True
        )
        self._agent_thread.start()

    def _run_agent(self, task: str, url: str) -> None:
        try:
            plans = self._orchestrator.run(
                task=task,
                url=url,
                on_captcha=self._on_captcha,
                on_plan_found=lambda p: self.after(0, lambda _p=p: self._log(f"Plan found: {_p.retailer} — {_p.plan_name}")),
                on_log=lambda msg: self.after(0, lambda m=msg: self._log(m)),
            )
            self.after(0, lambda: self._on_agent_done(plans))
        except Exception as exc:
            self.after(0, lambda e=exc: self._on_agent_error(e))

    def _stop_agent(self) -> None:
        self._running = False
        self._log("Stop requested. Agent will finish current action then halt.")
        # The orchestrator doesn't have an external stop flag yet; the thread
        # will complete its current iteration and exit naturally because
        # max_iterations is finite.
        self._start_btn.configure(state="normal")
        self._stop_btn.configure(state="disabled")

    def _on_agent_done(self, plans: list[ElectricityPlan]) -> None:
        self._running = False
        self._start_btn.configure(state="normal")
        self._stop_btn.configure(state="disabled")
        self._log(f"Agent finished. {len(plans)} plan(s) extracted.")
        if plans:
            self._on_plans_found(plans)

    def _on_agent_error(self, exc: Exception) -> None:
        self._running = False
        self._start_btn.configure(state="normal")
        self._stop_btn.configure(state="disabled")
        self._log(f"ERROR: {exc}")

    # ── CAPTCHA flow ───────────────────────────────────────────────────────────

    def _on_captcha(self) -> None:
        """Called from agent thread — show the CAPTCHA banner on the GUI thread."""
        self.after(0, self._show_captcha_banner)

    def _show_captcha_banner(self) -> None:
        self._captcha_frame.grid(row=3, column=0, padx=16, pady=4, sticky="ew")

    def _resume_after_captcha(self) -> None:
        self._captcha_frame.grid_remove()
        if self._orchestrator:
            self._orchestrator.signal_captcha_solved()
        self._log("CAPTCHA marked as solved. Resuming…")

    # ── Config builder ─────────────────────────────────────────────────────────

    def _build_config(self) -> Config:
        """Build a Config from the GUI fields, overriding env defaults."""
        base = get_config()
        provider = self._provider_var.get()
        model = self._model_entry.get().strip()
        api_key_or_url = self._api_key_entry.get().strip()

        import os
        if api_key_or_url:
            if provider == "ollama":
                os.environ["OLLAMA_BASE_URL"] = api_key_or_url
            elif provider == "glm":
                os.environ["GLM_API_KEY"] = api_key_or_url
            elif provider == "openai":
                os.environ["OPENAI_API_KEY"] = api_key_or_url
        if model:
            os.environ["LLM_MODEL"] = model
        os.environ["LLM_PROVIDER"] = provider

        # Re-read so the singleton picks up the overrides
        from power_analyser import config as cfg_module
        cfg_module._config = None  # reset singleton
        return get_config()

    # ── Log helper ─────────────────────────────────────────────────────────────

    def _log(self, message: str) -> None:
        self._log_box.configure(state="normal")
        self._log_box.insert("end", message + "\n")
        self._log_box.see("end")
        self._log_box.configure(state="disabled")
