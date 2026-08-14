"""Application composition root.  Importing this module has no runtime side effects."""

import argparse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from app.adapters.email_classifier import EmailClassifier
from app.adapters.fake import (
    FakeEphemeralEmailSecretStore,
    FakeFunPayAdapter,
    FakeGmailAdapter,
    FakePixelStormAdapter,
    FakeSecureStore,
)
from app.application.email_event_dispatcher import EmailEventDispatcher
from app.application.funpay_dispatcher import FunPayEventDispatcher, FunPayEventPoller
from app.application.gmail_watcher import GmailWatcher
from app.application.lease_guard import Clock, SystemClock
from app.application.otp_service import OTPService
from app.application.password_rotator import PasswordRotator
from app.application.pixelstorm_otp import PixelStormMaintenanceOtpService
from app.application.pixelstorm_security import PixelStormSecurityService
from app.application.rental_manager import RentalManager
from app.application.runtime import ApplicationRuntime
from app.application.scheduler import DurableScheduler
from app.application.security_monitor import SecurityMonitor
from app.application.startup_reconciliation import StartupReconciliation
from app.config.settings import Settings
from app.domain.ports import (
    EphemeralEmailSecretStore,
    FunPayPort,
    GmailPort,
    OwnerNotifier,
    PixelStormSecurityPort,
    SecureStorePort,
)
from app.persistence.classified_email_events import ClassifiedEmailRepository
from app.persistence.database import Database
from app.persistence.funpay_events import FunPayEventRepository
from app.persistence.repositories import Repository


def _require_runtime_adapter(adapter: object, label: str, settings: Settings) -> None:
    if settings.app_mode == "PRODUCTION":
        if not getattr(adapter, "production_safe", False):
            raise RuntimeError(f"PRODUCTION requires explicit production {label} adapter")
        return
    if not getattr(adapter, "sandbox_safe", False):
        raise RuntimeError(f"SANDBOX/DRY_RUN refuses non-sandbox {label} adapter")


def create_application(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    funpay: FunPayPort | None = None,
    pixelstorm: PixelStormSecurityPort | None = None,
    secrets: SecureStorePort | None = None,
    gmail: GmailPort | None = None,
    email_secrets: EphemeralEmailSecretStore | None = None,
    owner_notifier: OwnerNotifier | None = None,
    now: datetime | None = None,
    clock: Clock | None = None,
    lease_heartbeat_interval_seconds: float = 5.0,
    start_reconciliation: bool = True,
) -> ApplicationRuntime:
    """Compose the real application layer with explicitly safe adapters only."""
    settings = settings or Settings()
    settings.require_safe_mode()
    database = database or Database(settings.database_url)
    runtime_funpay = funpay or cast(FunPayPort, FakeFunPayAdapter())
    runtime_pixelstorm = pixelstorm or cast(PixelStormSecurityPort, FakePixelStormAdapter())
    runtime_secrets = secrets or FakeSecureStore()
    runtime_gmail = gmail or FakeGmailAdapter()
    runtime_email_secrets = email_secrets or FakeEphemeralEmailSecretStore()
    for adapter, label in (
        (runtime_funpay, "FunPay"),
        (runtime_pixelstorm, "Pixel Storm"),
        (runtime_secrets, "SecureStore"),
        (runtime_gmail, "Gmail"),
    ):
        _require_runtime_adapter(adapter, label, settings)

    repository = Repository(
        database,
        settings.maintenance_otp_correlation_seconds,
        settings.pixelstorm_password_change_correlation_seconds,
    )
    email_events = ClassifiedEmailRepository(
        database,
        settings.pixelstorm_password_change_correlation_seconds,
        settings.maintenance_otp_correlation_seconds,
    )
    password_rotator = PasswordRotator(email_events, runtime_email_secrets)
    maintenance_otp = PixelStormMaintenanceOtpService(email_events, runtime_email_secrets)
    runtime_clock = clock or SystemClock()
    security = PixelStormSecurityService(
        runtime_pixelstorm,
        runtime_secrets,
        owner_notifier,
        repository,
        maintenance_otp,
        password_rotator=password_rotator,
        clock=runtime_clock,
        lease_heartbeat_interval_seconds=lease_heartbeat_interval_seconds,
    )
    event_store = FunPayEventRepository(database)
    otp = OTPService(
        email_events,
        runtime_email_secrets,
        settings.otp_lookback_seconds,
        settings.otp_min_request_interval_seconds,
    )
    manager = RentalManager(
        repository,
        runtime_funpay,
        None,
        runtime_secrets,
        owner_notifier=owner_notifier,
        pixelstorm_security=security,
        otp_service=otp,
        message_receipts=event_store,
        clock=runtime_clock,
        lease_heartbeat_interval_seconds=lease_heartbeat_interval_seconds,
    )
    poller = FunPayEventPoller(runtime_funpay, event_store, owner_notifier)
    dispatcher = FunPayEventDispatcher(event_store, manager, otp, runtime_funpay, owner_notifier)
    watcher = GmailWatcher(
        runtime_gmail,
        EmailClassifier(),
        email_events,
        runtime_email_secrets,
        settings.email_secret_ttl_seconds,
        settings.password_reset_ttl_seconds,
    )
    startup = StartupReconciliation(repository, manager, runtime_funpay)
    runtime_now = now or datetime.now(UTC)
    if start_reconciliation:
        startup.run(runtime_now)
    return ApplicationRuntime(
        repository,
        manager,
        poller,
        dispatcher,
        watcher,
        EmailEventDispatcher(email_events, SecurityMonitor(repository)),
        DurableScheduler(repository, manager),
        startup,
        runtime_now,
        (runtime_funpay, runtime_pixelstorm, runtime_gmail),
    )


def run_application(argv: list[str] | None = None) -> None:
    """Production service entrypoint; ``--dry-run`` remains bounded for operators/tests."""
    parser = argparse.ArgumentParser(description="War Thunder rent-bot runtime")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--database-url")
    parser.add_argument("--runtime-dir")
    arguments = parser.parse_args(argv)
    if not arguments.dry_run:
        from app.production import create_production_application

        settings = Settings()
        if not settings.production_like:
            raise RuntimeError("PRODUCTION_RUNTIME_REQUIRED")
        runtime = create_production_application(settings)
        runtime.install_signal_handlers()
        try:
            runtime.run_forever()
        finally:
            runtime.shutdown()
        return
    from app.production import create_production_application

    values: dict[str, Any] = {"app_mode": "PRODUCTION_DRY_RUN", "dry_run": True}
    if arguments.database_url:
        values["database_url"] = arguments.database_url
    if arguments.runtime_dir:
        runtime_dir = Path(arguments.runtime_dir)
        values.update(
            runtime_dir=runtime_dir,
            secure_store_path=runtime_dir / "secure-store.vault",
            web_session_store_path=runtime_dir / "web-session.vault",
            log_path=runtime_dir / "logs" / "app.jsonl",
            backup_dir=runtime_dir / "backups",
        )
    runtime = create_production_application(Settings(**values))
    try:
        print({check.name: check.state for check in runtime.start()})
    finally:
        runtime.shutdown()


if __name__ == "__main__":
    run_application()
