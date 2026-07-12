"""Ollama provider — runs models locally via the Ollama HTTP API.

Requires Ollama to be running on the configured base URL (default:
http://localhost:11434).  Install from https://ollama.com and run the
desired model with `ollama pull <model>` before use.
"""

from __future__ import annotations

import base64
import json
import logging

import requests

from power_analyser.config import Config
from .base import LLMProvider

logger = logging.getLogger(__name__)

_GENERATE_ENDPOINT = "/api/generate"
_TIMEOUT_S = 120


class OllamaProvider(LLMProvider):
    """Calls the local Ollama server for completions."""

    def __init__(self, config: Config) -> None:
        self._base_url = config.ollama_base_url.rstrip("/")
        self._model = config.llm_model

    def complete(self, prompt: str) -> str:
        payload = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
        }
        return self._post(payload)

    def complete_with_image(self, prompt: str, image_bytes: bytes) -> str:
        """Send image as base64; falls back to text if the model lacks vision."""
        b64 = base64.b64encode(image_bytes).decode()
        payload = {
            "model": self._model,
            "prompt": prompt,
            "images": [b64],
            "stream": False,
        }
        try:
            return self._post(payload)
        except requests.HTTPError as exc:
            logger.warning("Vision call failed (%s); retrying as text-only.", exc)
            return self.complete(prompt)

    def _post(self, payload: dict) -> str:
        url = self._base_url + _GENERATE_ENDPOINT
        resp = requests.post(url, json=payload, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", "")
