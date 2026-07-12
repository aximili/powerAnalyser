"""Abstract base class for LLM providers.

All providers expose two methods:
  complete(prompt)                  — text-only completion
  complete_with_image(prompt, img)  — vision-capable completion (for screenshots)

If a provider does not support vision, ``complete_with_image`` should fall
back to ``complete`` and log a warning.

Create a provider with the factory function:
  from power_analyser.agent.llm.base import create_provider
  provider = create_provider(get_config())
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from power_analyser.config import Config

logger = logging.getLogger(__name__)


class LLMProvider(ABC):
    """Abstract interface for a language model backend."""

    @abstractmethod
    def complete(self, prompt: str) -> str:
        """Return a text completion for the given prompt."""

    @abstractmethod
    def complete_with_image(self, prompt: str, image_bytes: bytes) -> str:
        """Return a completion that can reason about an attached image.

        Falls back to text-only if the backend has no vision capability.
        """


def create_provider(config: Config) -> LLMProvider:
    """Instantiate the appropriate LLM provider based on ``config.llm_provider``."""
    provider_name = config.llm_provider.lower()

    if provider_name == "ollama":
        from .ollama_provider import OllamaProvider
        return OllamaProvider(config)

    if provider_name == "glm":
        from .glm_provider import GLMProvider
        return GLMProvider(config)

    if provider_name == "openai":
        from .openai_provider import OpenAIProvider
        return OpenAIProvider(config)

    raise ValueError(
        f"Unknown LLM provider '{provider_name}'. "
        f"Supported: ollama, glm, openai"
    )
