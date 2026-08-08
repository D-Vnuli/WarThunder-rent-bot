from datetime import UTC, datetime, timedelta, timezone

import pytest


def test_sqlite_datetime_round_trip_is_utc(core, now):
    repository, _, _, _ = core
    account_id = repository.add_account("WT01", now)
    assert repository.get_account(account_id).created_at == now
    assert repository.get_account(account_id).created_at.tzinfo is UTC


def test_datetime_normalizes_non_utc_and_rejects_naive(core, now):
    repository, _, _, _ = core
    offset = timezone(timedelta(hours=3))
    account_id = repository.add_account("WT01", now.astimezone(offset))
    assert repository.get_account(account_id).created_at == now
    with pytest.raises(ValueError):
        repository.add_account("WT02", datetime(2026, 8, 8, 12, 0))
