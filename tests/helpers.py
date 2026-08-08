from datetime import datetime


def create_test_account(repository, funpay, code: str, now: datetime) -> str:
    """Create a lifecycle-ready account with an explicit durable test lot."""
    account_id = repository.add_account(code, now)
    lot_id = f"test-lot-{account_id}"
    repository.add_account_lot(account_id, lot_id, now)
    funpay.set_lot_state(lot_id, enabled=True)
    secrets = getattr(repository, "_test_secret_store", None)
    if secrets is not None:
        secrets.set_current_credentials(account_id, f"test-login-{account_id}", "test-password")
    return account_id
