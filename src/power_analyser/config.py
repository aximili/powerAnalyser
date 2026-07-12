"""Application-wide configuration loaded from environment variables / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # ── LLM ───────────────────────────────────────────────────────────────────
    llm_provider: str = field(
        default_factory=lambda: os.getenv("LLM_PROVIDER", "ollama")
    )
    llm_model: str = field(
        default_factory=lambda: os.getenv("LLM_MODEL", "llama3.2")
    )
    ollama_base_url: str = field(
        default_factory=lambda: os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    )
    glm_api_key: str = field(
        default_factory=lambda: os.getenv("GLM_API_KEY", "")
    )
    openai_api_key: str = field(
        default_factory=lambda: os.getenv("OPENAI_API_KEY", "")
    )
    openai_base_url: str = field(
        default_factory=lambda: os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    )

    # ── Agent ─────────────────────────────────────────────────────────────────
    max_agent_iterations: int = field(
        default_factory=lambda: int(os.getenv("MAX_AGENT_ITERATIONS", "25"))
    )
    agent_headless: bool = field(
        default_factory=lambda: os.getenv("AGENT_HEADLESS", "false").lower() == "true"
    )

    # ── Paths ─────────────────────────────────────────────────────────────────
    data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("DATA_DIR", "data"))
    )


_config: Config | None = None


def get_config() -> Config:
    """Return the singleton Config instance (lazy-initialised)."""
    global _config
    if _config is None:
        _config = Config()
    return _config
