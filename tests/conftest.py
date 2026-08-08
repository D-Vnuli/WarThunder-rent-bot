from datetime import UTC, datetime

import pytest

from app.adapters.fake import FakeFunPayAdapter, FakeGaijinController, FakeSecureStore
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
    return repository, RentalManager(repository, funpay, gaijin, secrets), funpay, gaijin
