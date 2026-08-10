from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeMode:
    SANDBOX = "SANDBOX"
    DRY_RUN = "DRY_RUN"
    PRODUCTION = "PRODUCTION"


class Settings(BaseSettings):
    app_mode: str = RuntimeMode.SANDBOX
    dry_run: bool = True
    database_url: str = "sqlite:///data/war_thunder_rent_bot.db"
    otp_lookback_seconds: int = 120
    otp_min_request_interval_seconds: int = 30
    email_secret_ttl_seconds: int = 300
    password_reset_ttl_seconds: int = 900
    pixelstorm_password_change_correlation_seconds: int = 900
    maintenance_otp_correlation_seconds: int = 300
    live_pixelstorm_enabled: bool = False
    live_pixelstorm_destructive_confirmed: bool = False
    gmail_oauth_client_id: str | None = None
    gmail_allowed_sender: str | None = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def require_safe_mode(self) -> None:
        if self.app_mode.upper() not in {RuntimeMode.SANDBOX, RuntimeMode.DRY_RUN}:
            raise RuntimeError("PHASE 5 production runtime is not enabled")
        if not self.dry_run:
            raise RuntimeError("SANDBOX/DRY_RUN requires DRY_RUN=true")
