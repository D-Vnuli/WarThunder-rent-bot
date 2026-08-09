"""Owner-gated guard for future live Pixel Storm validation commands."""

from app.config.settings import Settings


class PixelStormLiveGuard:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def require_read_only(self) -> None:
        if not self._settings.live_pixelstorm_enabled:
            raise RuntimeError("LIVE_PIXELSTORM_READ_ONLY is disabled")

    def require_destructive(self) -> None:
        self.require_read_only()
        if not self._settings.live_pixelstorm_destructive_confirmed:
            raise RuntimeError("explicit destructive Pixel Storm confirmation is required")
