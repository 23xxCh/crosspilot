"""Local Ollama vision fallback using the existing structured prompts."""
from __future__ import annotations

from typing import Optional

from .agnes import AgnesProvider
from .support import ProviderResponseError


class OllamaVisionProvider(AgnesProvider):
    """Review images locally, one at a time for reliable Qwen3-VL output."""

    def __init__(self, api_key: str, *, base_url: str, model: str) -> None:
        super().__init__(
            api_key,
            base_url=base_url,
            text_model=model,
            vision_model=model,
            image_model=model,
        )
        self.MODEL = model

    def assess_image(
        self,
        image_url: str,
        *,
        confirmation: bool = False,
        policy: str = "general",
        retries: int = 1,
    ) -> Optional[dict]:
        local_image = str(image_url or "").strip()
        if not local_image.startswith("data:"):
            local_image = self._download_image_data_url(local_image)
        return super().assess_image(
            local_image,
            confirmation=confirmation,
            policy=policy,
            retries=retries,
        )

    def assess_images(
        self,
        image_urls: list[str],
        *,
        policy: str = "general",
        retries: int = 1,
    ) -> list[dict]:
        results: list[dict] = []
        for image_url in image_urls:
            result = self.assess_image(
                image_url,
                policy=policy,
                retries=retries,
            )
            if not isinstance(result, dict):
                raise ProviderResponseError(
                    "Ollama 单图审查未返回结构化结果",
                    provider="ollama",
                    operation="vision",
                )
            results.append(result)
        return results


__all__ = ["OllamaVisionProvider"]
