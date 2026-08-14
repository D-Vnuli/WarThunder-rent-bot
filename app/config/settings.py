"""Validated, secret-safe runtime configuration."""

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeMode:
    SANDBOX = "SANDBOX"
    DRY_RUN = "DRY_RUN"  # Compatibility alias retained for the accepted PHASE 5 sandbox.
    PRODUCTION_DRY_RUN = "PRODUCTION_DRY_RUN"
    PRODUCTION = "PRODUCTION"


RuntimeModeValue = Literal["SANDBOX", "DRY_RUN", "PRODUCTION_DRY_RUN", "PRODUCTION"]


class Settings(BaseSettings):
    app_mode: RuntimeModeValue = "SANDBOX"
    dry_run: bool = True
    allow_live_operations: bool = False
    allow_pixelstorm_security_mutations: bool = False
    allow_funpay_mutations: bool = False
    database_url: str = "sqlite:///data/war_thunder_rent_bot.db"
    runtime_dir: Path = Path("runtime")
    secure_store_path: Path = Path("runtime/secure-store.vault")
    web_session_store_path: Path = Path("runtime/web-session.vault")
    log_path: Path = Path("runtime/logs/app.jsonl")
    backup_dir: Path = Path("runtime/backups")
    log_retention: int = Field(default=5, ge=1)
    backup_retention: int = Field(default=7, ge=1)
    cleanup_retention_days: int = Field(default=30, ge=1)
    normal_worker_lease_seconds: int = Field(default=30, gt=0)
    recovery_lease_seconds: int = Field(default=300, gt=0)
    lease_heartbeat_interval_seconds: float = Field(default=5.0, gt=0)
    external_timeout_seconds: float = Field(default=15.0, gt=0)
    funpay_poll_interval_seconds: float = Field(default=10.0, gt=0)
    gmail_poll_interval_seconds: float = Field(default=10.0, gt=0)
    playwright_timeout_seconds: float = Field(default=15.0, gt=0)
    owner_notifier_timeout_seconds: float = Field(default=10.0, gt=0)
    owner_notification_cooldown_seconds: int = Field(default=300, gt=0)
    otp_lookback_seconds: int = Field(default=120, gt=0)
    otp_min_request_interval_seconds: int = Field(default=30, gt=0)
    email_secret_ttl_seconds: int = Field(default=300, gt=0)
    password_reset_ttl_seconds: int = Field(default=900, gt=0)
    pixelstorm_password_change_correlation_seconds: int = Field(default=900, gt=0)
    maintenance_otp_correlation_seconds: int = Field(default=300, gt=0)
    live_pixelstorm_enabled: bool = False
    live_pixelstorm_destructive_confirmed: bool = False
    gmail_oauth_client_id: str | None = None
    gmail_allowed_sender: str | None = None
    # Secret-bearing values are deliberately excluded from repr and diagnostics.
    funpay_session: str | None = Field(default=None, repr=False)
    gmail_refresh_token: str | None = Field(default=None, repr=False)
    gmail_oauth_client_secret: str | None = Field(default=None, repr=False)
    pixelstorm_password: str | None = Field(default=None, repr=False)
    owner_notifier_token: str | None = Field(default=None, repr=False)
    golden_key: str | None = Field(default=None, repr=False)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def _validate_runtime_safety(self) -> "Settings":
        if not self.database_url.startswith("sqlite:///"):
            raise ValueError("DATABASE_URL must be an absolute SQLite URL")
        if self.lease_heartbeat_interval_seconds >= self.normal_worker_lease_seconds:
            raise ValueError("lease heartbeat interval must be shorter than normal worker lease")
        if self.external_timeout_seconds >= self.normal_worker_lease_seconds:
            raise ValueError("external timeout must be shorter than normal worker lease")
        if self.app_mode in {RuntimeMode.SANDBOX, RuntimeMode.DRY_RUN, RuntimeMode.PRODUCTION_DRY_RUN} and not self.dry_run:
            raise ValueError("SANDBOX and dry-run modes require DRY_RUN=true")
        if self.app_mode == RuntimeMode.PRODUCTION and self.dry_run:
            raise ValueError("PRODUCTION requires DRY_RUN=false")
        return self

    @property
    def production_like(self) -> bool:
        return self.app_mode in {RuntimeMode.PRODUCTION_DRY_RUN, RuntimeMode.PRODUCTION}

    def require_safe_mode(self) -> None:
        if self.app_mode == RuntimeMode.PRODUCTION:
            if not self.allow_live_operations:
                raise RuntimeError(
                    "production runtime is not enabled: ALLOW_LIVE_OPERATIONS=true is required"
                )
            return
        if self.app_mode not in {RuntimeMode.SANDBOX, RuntimeMode.DRY_RUN, RuntimeMode.PRODUCTION_DRY_RUN}:
            raise RuntimeError("unknown runtime mode")
        if not self.dry_run:
            raise RuntimeError("SANDBOX/DRY_RUN requires DRY_RUN=true")

    def safe_summary(self) -> dict[str, object]:
        return {
            "mode": self.app_mode,
            "dry_run": self.dry_run,
            "allow_live_operations": self.allow_live_operations,
            "allow_pixelstorm_security_mutations": self.allow_pixelstorm_security_mutations,
            "allow_funpay_mutations": self.allow_funpay_mutations,
            "database_url": self.database_url,
            "runtime_dir": str(self.runtime_dir),
            "log_path": str(self.log_path),
            "backup_dir": str(self.backup_dir),
            "normal_worker_lease_seconds": self.normal_worker_lease_seconds,
            "lease_heartbeat_interval_seconds": self.lease_heartbeat_interval_seconds,
            "external_timeout_seconds": self.external_timeout_seconds,
            "funpay_configured": self.funpay_session is not None,
            "gmail_configured": self.gmail_refresh_token is not None,
            "secure_store_configured": bool(self.golden_key),
            "web_session_store_configured": self.web_session_store_path.exists(),
            "owner_notifier_configured": self.owner_notifier_token is not None,
        }
