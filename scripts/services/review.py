"""Image review service for watermark, brand overlay, and person detection.
使用 model_provider 进行图审，与具体模型解耦。
"""
from pipeline_log import log as _log
from model_provider import get_provider


class ImageReviewService:
    """Image remediation detection using model_provider.
    自动路由到配置的 vision provider（默认 Agnes）。"""

    def __init__(self, http_session=None, base_url=None):
        """初始化图审服务。

        Args:
            http_session: 保留参数以兼容旧接口
            base_url: 保留参数以兼容旧接口
        """
        self._provider = get_provider()

    def review(self, image_url, model=None, max_retries=3):
        """Review a single image using model_provider.

        Args:
            image_url: 图片 URL
            model: 保留参数以兼容旧接口
            max_retries: 最大重试次数

        Returns:
            True (needs remediation), False (clean), or None (failed)
        """
        return self._provider.call_vision(image_url)

    def review_once(self, image_url, model=None, timeout=15):
        """Single vision call, no retries.
        Public interface for high-concurrency batch review."""
        return self._provider.call_vision(image_url)
