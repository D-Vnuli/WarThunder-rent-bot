import pytest

from app.application.pixelstorm_live import PixelStormLiveGuard
from app.config.settings import Settings


def test_live_guard_denies_by_default_and_allows_only_explicit_confirmation():
    guard = PixelStormLiveGuard(Settings(live_pixelstorm_enabled=False))
    with pytest.raises(RuntimeError):
        guard.require_read_only()
    read_only = PixelStormLiveGuard(Settings(live_pixelstorm_enabled=True))
    read_only.require_read_only()
    with pytest.raises(RuntimeError):
        read_only.require_destructive()
    PixelStormLiveGuard(
        Settings(live_pixelstorm_enabled=True, live_pixelstorm_destructive_confirmed=True)
    ).require_destructive()
