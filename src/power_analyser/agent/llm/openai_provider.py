"""OpenAI-compatible provider.

Works with:
  - OpenAI API   (OPENAI_API_KEY + default base URL)
  - Local servers (LM Studio, vLLM, llama.cpp) via OPENAI_BASE_URL override
  - Other OpenAI-compatible APIs

Requires: pip install openai
"""

from __future__ import annotations

import base64
import logging

from power_analyser.config import Config
from .base import LLMProvider

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    """Calls any OpenAI-compatible chat completions endpoint."""

    def __init__(self, config: Config) -> None:
        try:
            from openai import OpenAI  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "The 'openai' package is required for the OpenAI provider. "
                "Install it with: pip install openai"
            ) from exc

        self._client = OpenAI(
            api_key=config.openai_api_key or "placeholder",
            base_url=config.openai_base_url,
        )
        self._model = config.llm_model

    def complete(self, prompt: str) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content or ""

    def complete_with_image(self, prompt: str, image_bytes: bytes) -> str:
        """Send a screenshot alongside the prompt using the vision API format."""
        b64 = base64.b64encode(image_bytes).decode()

        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{b64}",
                                    "detail": "high",
                                },
                            },
                        ],
                    }
                ],
            )
            return resp.choices[0].message.content or ""
        except Exception as exc:
            logger.warning("Vision call failed (%s); retrying as text-only.", exc)
            return self.complete(prompt)
