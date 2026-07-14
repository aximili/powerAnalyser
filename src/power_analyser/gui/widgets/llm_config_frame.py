"""Reusable LLM provider configuration frame.

Shared by the **Agent** tab (browser automation) and the **Manual** tab
(screenshot extraction).  Both tabs receive the *same* set of tkinter
``StringVar`` objects (created once in :class:`PowerAnalyserApp`), so editing
the provider/model/API-key in one tab instantly updates the other.

Build the provider config with :meth:`LLMConfigFrame.build_config`, which
mirrors the environment variables consumed by :func:`create_provider`.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import customtkinter as ctk

from power_analyser.config import Config, get_config

# Placeholder shown in the API-key / base-URL field for each provider.
_PLACEHOLDERS = {
    "ollama": "Ollama base URL (e.g. http://localhost:11434)",
    "glm": "Zhipu AI API key",
    "openai": "API key, or a base URL for a local OpenAI-compatible server",
}


def default_llm_vars() -> dict[str, ctk.StringVar]:
    """Create a fresh set of shared tkinter variables (call after Tk root exists)."""
    return {
        "provider": ctk.StringVar(value="ollama"),
        "model": ctk.StringVar(value=""),
        "apikey": ctk.StringVar(value=""),
    }


def _env_credential(cfg: Config, provider: str) -> str:
    """Pick the relevant ``.env`` credential for *provider*.

    Used to pre-fill the API key / base-URL textbox on first run.  The OpenAI
    base-URL default is intentionally NOT echoed (only a real API key is).
    """
    if provider == "ollama":
        return cfg.ollama_base_url
    if provider == "glm":
        return cfg.glm_api_key
    if provider == "openai":
        return cfg.openai_api_key
    return ""


class LLMConfigFrame(ctk.CTkFrame):
    """Provider / model / API-key inputs backed by shared string variables."""

    def __init__(
        self,
        parent,
        shared_vars: Optional[dict[str, ctk.StringVar]] = None,
        **kwargs,
    ) -> None:
        super().__init__(parent, **kwargs)
        self._vars = shared_vars if shared_vars is not None else default_llm_vars()
        self.columnconfigure((1, 3), weight=1)
        self._build_ui()

    # ── Layout ─────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        ctk.CTkLabel(
            self, text="LLM Configuration", font=ctk.CTkFont(size=13, weight="bold")
        ).grid(row=0, column=0, columnspan=4, padx=8, pady=(8, 4), sticky="w")

        ctk.CTkLabel(self, text="Provider:").grid(
            row=1, column=0, padx=8, pady=4, sticky="w"
        )
        provider_menu = ctk.CTkOptionMenu(
            self,
            variable=self._vars["provider"],
            values=["ollama", "glm", "openai"],
            command=self._on_provider_change,
        )
        provider_menu.grid(row=1, column=1, padx=8, pady=4, sticky="ew")

        ctk.CTkLabel(self, text="Model:").grid(
            row=1, column=2, padx=8, pady=4, sticky="w"
        )
        self._model_entry = ctk.CTkEntry(
            self,
            textvariable=self._vars["model"],
            placeholder_text="e.g. llama3.2, gemma2:12b, gpt-4o",
        )
        self._model_entry.grid(row=1, column=3, padx=8, pady=4, sticky="ew")

        ctk.CTkLabel(self, text="API Key / Base URL:").grid(
            row=2, column=0, padx=8, pady=4, sticky="w"
        )
        self._apikey_entry = ctk.CTkEntry(
            self,
            textvariable=self._vars["apikey"],
            placeholder_text=_PLACEHOLDERS["ollama"],
        )
        self._apikey_entry.grid(row=2, column=1, columnspan=3, padx=8, pady=4, sticky="ew")

        # Keep the placeholder in sync with the chosen provider on first paint.
        self._on_provider_change(self._vars["provider"].get())

    # ── Provider change ────────────────────────────────────────────────────────

    def _on_provider_change(self, value: str) -> None:
        self._apikey_entry.configure(placeholder_text=_PLACEHOLDERS.get(value, ""))

    # ── Settings persistence ───────────────────────────────────────────────────

    def apply_settings(self, settings: dict[str, Any]) -> None:
        """Restore provider/model/api-key, falling back to ``.env`` defaults.

        Saved GUI values win; otherwise the values loaded from ``.env`` into
        :class:`Config` are used so the textboxes are populated on first run.
        """
        cfg = get_config()
        provider = settings.get("llm_provider") or cfg.llm_provider
        self._vars["provider"].set(provider)

        model = settings.get("llm_model") or cfg.llm_model
        if model:
            self._vars["model"].set(model)

        apikey = settings.get("api_key_or_url") or _env_credential(cfg, provider)
        if apikey:
            self._vars["apikey"].set(apikey)

        self._on_provider_change(provider)

    def collect_state(self, settings: dict[str, Any]) -> None:
        """Write the current values back into a settings dict."""
        settings["llm_provider"] = self._vars["provider"].get()
        settings["llm_model"] = self._vars["model"].get().strip()
        settings["api_key_or_url"] = self._vars["apikey"].get().strip()

    # ── Config builder ─────────────────────────────────────────────────────────

    def build_config(self) -> Config:
        """Materialise a :class:`Config` from the current field values.

        Sets the relevant environment variables (so the provider classes pick
        them up) and resets the :data:`Config` singleton.
        """
        provider = self._vars["provider"].get()
        model = self._vars["model"].get().strip()
        apikey = self._vars["apikey"].get().strip()

        if apikey:
            if provider == "ollama":
                os.environ["OLLAMA_BASE_URL"] = apikey
            elif provider == "glm":
                os.environ["GLM_API_KEY"] = apikey
            elif provider == "openai":
                # A URL => local OpenAI-compatible server; otherwise an API key.
                if apikey.startswith(("http://", "https://")):
                    os.environ["OPENAI_BASE_URL"] = apikey
                else:
                    os.environ["OPENAI_API_KEY"] = apikey
        if model:
            os.environ["LLM_MODEL"] = model
        os.environ["LLM_PROVIDER"] = provider

        from power_analyser import config as cfg_module

        cfg_module._config = None  # reset singleton so overrides take effect
        return get_config()
