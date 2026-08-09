from datetime import UTC, datetime
from typing import cast

from app.adapters.fake import (
    FakeEphemeralEmailSecretStore,
    FakeFunPayAdapter,
    FakePixelStormAdapter,
    FakeSecureStore,
)
from app.application.password_rotator import PasswordRotator
from app.application.pixelstorm_otp import PixelStormMaintenanceOtpService
from app.application.pixelstorm_security import PixelStormSecurityService
from app.application.rental_manager import RentalManager
from app.application.startup_reconciliation import StartupReconciliation
from app.config.settings import Settings
from app.domain.ports import (
    FunPayPort,
    OwnerNotifier,
    PixelStormSecurityPort,
    SecureStorePort,
)
from app.persistence.classified_email_events import ClassifiedEmailRepository
from app.persistence.database import Database
from app.persistence.repositories import Repository


def create_application(
    *,
    database: Database | None = None,
    funpay: FunPayPort | None = None,
    pixelstorm: PixelStormSecurityPort | None = None,
    secrets: SecureStorePort | None = None,
    owner_notifier: OwnerNotifier | None = None,
    now: datetime | None = None,
) -> Database:
    """Compose only DRY_RUN fakes until a real adapter is explicitly introduced."""
    settings = Settings()
    settings.require_safe_mode()
    database = database or Database(settings.database_url)
    # Production schema is owned exclusively by Alembic migrations.
    repository = Repository(database)
    runtime_funpay: FunPayPort = funpay if funpay is not None else cast(FunPayPort, FakeFunPayAdapter())
    runtime_secrets = secrets or FakeSecureStore()
    runtime_pixelstorm = pixelstorm or FakePixelStormAdapter()
    email_events = ClassifiedEmailRepository(
        database, settings.pixelstorm_password_change_correlation_seconds
    )
    email_secrets = FakeEphemeralEmailSecretStore()
    maintenance_otp = PixelStormMaintenanceOtpService(email_events, email_secrets)
    manager = RentalManager(
        repository,
        runtime_funpay,
        None,
        runtime_secrets,
        owner_notifier=owner_notifier,
        pixelstorm_security=PixelStormSecurityService(
            runtime_pixelstorm,
            runtime_secrets,
            owner_notifier,
            repository,
            maintenance_otp,
            password_rotator=PasswordRotator(email_events, email_secrets),
        ),
    )
    StartupReconciliation(repository, manager, runtime_funpay).run(now or datetime.now(UTC))
    return database
