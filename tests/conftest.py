from datetime import UTC, datetime

import pytest

from app.adapters.fake import (
    FakeFunPayAdapter,
    FakeGaijinController,
    FakePixelStormAdapter,
    FakeSecureStore,
)
from app.application.pixelstorm_security import PixelStormSecurityService
from app.application.rental_manager import RentalManager
from app.persistence.database import Database
from app.persistence.repositories import Repository


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
def core():
    database = Database("sqlite://")
    database.create_schema()
    repository = Repository(database)
    funpay, gaijin, secrets = FakeFunPayAdapter(), FakeGaijinController(), FakeSecureStore()
    repository._test_secret_store = secrets  # type: ignore[attr-defined]
    pixelstorm = FakePixelStormAdapter()
    manager = RentalManager(
        repository,
        funpay,
        gaijin,
        secrets,
        pixelstorm_security=PixelStormSecurityService(pixelstorm, secrets, repository=repository),
    )
    repository._test_manager = manager  # type: ignore[attr-defined]
    return repository, manager, funpay, gaijin
