"""Zhipu AI GLM provider.

Supports GLM-4 (text) and GLM-4V (vision).  Requires the ``zhipuai`` SDK:
  pip install zhipuai

Obtain an API key from https://open.bigmodel.cn and set GLM_API_KEY in .env.
"""

from __future__ import annotations

import base64
import logging

from power_analyser.config import Config
from .base import LLMProvider

logger = logging.getLogger(__name__)

# GLM-4V is the vision-capable variant; GLM-4 is text-only.
_VISION_MODEL_SUFFIX = "v"


class GLMProvider(LLMProvider):
    """Calls the Zhipu AI GLM API."""

    def __init__(self, config: Config) -> None:
        try:
            from zhipuai import ZhipuAI  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "The 'zhipuai' package is required for the GLM provider. "
                "Install it with: pip install zhipuai"
            ) from exc

        self._client = ZhipuAI(api_key=config.glm_api_key)
        self._model = config.llm_model

    def complete(self, prompt: str) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content or ""

    def complete_with_image(self, prompt: str, image_bytes: bytes) -> str:
        """Use the GLM-4V vision model for image reasoning."""
        b64 = base64.b64encode(image_bytes).decode()

        # Auto-select the vision variant of the configured model if needed
        vision_model = self._model
        if not vision_model.lower().endswith(_VISION_MODEL_SUFFIX):
            vision_model = vision_model + _VISION_MODEL_SUFFIX
            logger.debug("Switched to vision model: %s", vision_model)

        try:
            resp = self._client.chat.completions.create(
                model=vision_model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{b64}"},
                            },
                        ],
                    }
                ],
            )
            return resp.choices[0].message.content or ""
        except Exception as exc:
            logger.warning("GLM vision call failed (%s); retrying as text-only.", exc)
            return self.complete(prompt)
