from datetime import datetime

from app.adapters.fake import FakePixelStormAdapter
from app.application.pixelstorm_security import PixelStormSecurityService


def fake_pixelstorm_security(repository, secrets, notifier=None):
    return PixelStormSecurityService(
        FakePixelStormAdapter(), secrets, notifier, repository
    )


def create_pixelstorm_manager(repository, funpay, secrets, *, notifier=None, otp_service=None):
    """Explicit Pixel Storm-only test composition for legacy lifecycle tests."""
    from app.application.rental_manager import RentalManager

    return RentalManager(
        repository,
        funpay,
        None,
        secrets,
        owner_notifier=notifier,
        otp_service=otp_service,
        pixelstorm_security=fake_pixelstorm_security(repository, secrets, notifier),
    )


def create_test_account(repository, funpay, code: str, now: datetime) -> str:
    """Create a lifecycle-ready account with an explicit durable test lot."""
    account_id = repository.add_account(code, now)
    lot_id = f"test-lot-{account_id}"
    repository.add_account_lot(account_id, lot_id, now)
    funpay.set_lot_state(lot_id, enabled=True)
    secrets = getattr(repository, "_test_secret_store", None)
    if secrets is not None:
        secrets.set_current_credentials(account_id, f"test-login-{account_id}", "test-password")
    security = getattr(getattr(repository, "_test_manager", None), "_pixelstorm_security", None)
    if security is not None and secrets is not None:
        credentials = secrets.get_current_credentials(account_id)
        if credentials is not None:
            security._pixelstorm.set_credentials(account_id, *credentials)  # type: ignore[attr-defined]
    return account_id
