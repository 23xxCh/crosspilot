"""Image review service — watermark detection via DMXAPI vision models."""
import time
from pipeline_log import log as _log

REVIEW_PROMPT = (
    "Look at this image. Answer YES only if it has a SELLER WATERMARK: semi-transparent/faint text "
    "(like a store ID, e.g. 'liazh-93', 'Constituen78') repeated or tiled across the image, or a car brand logo "
    "(BMW/Toyota/Honda) overlaid on the photo.\n\n"
    "Answer NO for these cases:\n"
    "- Product spec text: '2PCS', '1Set', '1PC', dimensions like '48*30CM' (solid text, single instance, in a corner)\n"
    "- Product feature callouts or marketing banners: '7 COLOR', 'Soft and Comfortable', feature icons\n"
    "- Text that is part of the product itself: emblems/badges like 'SPORT', '4x4', 'LIMITED EDITION' printed ON the product\n"
    "- Clean images with no text overlay\n\n"
    "Answer YES or NO only."
)

FALLBACK_MODEL = "gemini-3.1-flash-lite-image"


class ImageReviewService:
    """Watermark detection service using DMXAPI vision models.
    Three-level fallback: MiMo fast → MiMo slow → Gemini."""

    def __init__(self, http_session):
        self._http = http_session

    def _vision_call(self, model, image_url, timeout=25):
        """Single vision call. Returns True/False/None."""
        try:
            r = self._http.post(
                'https://www.dmxapi.cn/v1/chat/completions',
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {"type": "text", "text": REVIEW_PROMPT}
                    ]}],
                    "max_completion_tokens": 32
                },
                timeout=timeout + 5
            )
            if not r.ok:
                return None
            content = r.json().get('choices', [{}])[0].get('message', {}).get('content', '')
            return content.strip().upper().startswith('YES') if content else None
        except Exception as e:
            _log.warn("vision_call 异常", model=model, error=str(e))
            return None

    def review(self, image_url, model="mimo-v2.5", max_retries=3):
        """Review a single image with three-level fallback.
        Returns True (watermark), False (clean), or None (all models failed)."""
        # Level 1: fast retries
        for attempt in range(max_retries):
            r = self._vision_call(model, image_url, timeout=8)
            if r is not None:
                return r
            if attempt < max_retries - 1:
                time.sleep(3 * (attempt + 1))

        # Level 2: slow retries
        for attempt in range(2):
            r = self._vision_call(model, image_url, timeout=15)
            if r is not None:
                return r
            if attempt < 1:
                time.sleep(5)

        # Level 3: Gemini fallback
        for attempt in range(2):
            r = self._vision_call(FALLBACK_MODEL, image_url, timeout=15)
            if r is not None:
                return r
            if attempt < 1:
                time.sleep(5)

        return None

    def review_once(self, image_url, model="mimo-v2.5", timeout=15):
        """Single vision call, no retries. Returns True/False/None.
        Public interface for high-concurrency batch review."""
        return self._vision_call(model, image_url, timeout)
