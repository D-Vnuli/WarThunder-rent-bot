from secrets import token_urlsafe


class PixelStormPasswordGenerator:
    """Configurable cryptographic policy; values never leave SecureStore."""

    def __init__(self, token_bytes: int = 32) -> None:
        if token_bytes < 24:
            raise ValueError("Pixel Storm password entropy must be at least 24 bytes")
        self._token_bytes = token_bytes

    def generate(self) -> str:
        return token_urlsafe(self._token_bytes)
