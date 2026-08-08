from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_mode: str = "development"
    dry_run: bool = True
    database_url: str = "sqlite:///data/war_thunder_rent_bot.db"
    otp_lookback_seconds: int = 120
    otp_min_request_interval_seconds: int = 30
    email_secret_ttl_seconds: int = 300
    password_reset_ttl_seconds: int = 900
    gmail_oauth_client_id: str | None = None
    gmail_allowed_sender: str | None = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def require_safe_mode(self) -> None:
        if not self.dry_run:
            raise RuntimeError("PHASE 1 permits only DRY_RUN=true")
