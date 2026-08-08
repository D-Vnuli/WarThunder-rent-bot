from datetime import UTC, datetime
from typing import cast

from app.adapters.fake import FakeFunPayAdapter, FakeGaijinController, FakeSecureStore
from app.application.rental_manager import RentalManager
from app.application.startup_reconciliation import StartupReconciliation
from app.config.settings import Settings
from app.domain.ports import FunPayPort, GaijinPort, OwnerNotifier, SecureStorePort
from app.persistence.database import Database
from app.persistence.repositories import Repository


def create_application(
    *,
    database: Database | None = None,
    funpay: FunPayPort | None = None,
    gaijin: GaijinPort | None = None,
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
    manager = RentalManager(
        repository,
        runtime_funpay,
        gaijin or FakeGaijinController(),
        secrets or FakeSecureStore(),
        owner_notifier=owner_notifier,
    )
    StartupReconciliation(repository, manager, runtime_funpay).run(now or datetime.now(UTC))
    return database
