"""Image generation service — watermark removal via DMXAPI image generation."""
from dmx_client import generate_image as _dmx_gen


class ImageGenService:
    """Image generation (watermark removal) using DMXAPI wan2.7 / doubao models.
    Delegates to dmx_client for the actual API calls."""

    def __init__(self, http_session):
        self._http = http_session

    def generate(self, image_url):
        """Generate a watermark-free version of the image. Returns new URL or None."""
        return _dmx_gen(self._http, image_url)
