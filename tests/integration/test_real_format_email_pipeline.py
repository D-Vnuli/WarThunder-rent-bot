from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.adapters.email_classifier import EmailClassifier, parse_sanitized_eml
from app.adapters.fake import FakeEphemeralEmailSecretStore, FakeGmailAdapter
from app.application.email_event_dispatcher import EmailEventDispatcher
from app.application.gmail_watcher import GmailWatcher
from app.application.otp_service import OTPService
from app.application.password_rotator import PasswordRotator
from app.application.security_monitor import SecurityMonitor
from app.domain.models import OrderInput, RawEmail
from app.domain.states import (
    AccountStatus,
    EmailMessageType,
    OperationKind,
    OperationStatus,
    RentalStatus,
)
from app.persistence.classified_email_events import ClassifiedEmailRepository
from app.persistence.models import ClassifiedEmailEventRow, OperationRow, SecurityEventRow
from tests.helpers import create_test_account

FIXTURES = Path(__file__).parents[2] / "fixtures" / "email"
NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)


def _active_rental(core, now=NOW):
    repository, manager, funpay, _ = core
    account_id = create_test_account(repository, funpay, "WT01", now)
    started = manager.accept_order(OrderInput("real-format-order", "buyer", "1H", 3600), now)
    manager.run_operations(now)
    manager.run_operations(now)
    assert started.rental_id is not None
    return account_id, started.rental_id


def _services(core, messages):
    repository, _, _, _ = core
    events = ClassifiedEmailRepository(repository.db)
    secrets = FakeEphemeralEmailSecretStore()
    watcher = GmailWatcher(FakeGmailAdapter(messages), EmailClassifier(), events, secrets, 300, 900)
    dispatcher = EmailEventDispatcher(events, SecurityMonitor(repository))
    return events, secrets, watcher, dispatcher


def _fixture_raw(name, message_id, account_id, received_at=NOW):
    return parse_sanitized_eml(
        (FIXTURES / name).read_bytes(),
        gmail_message_id=message_id,
        received_at=received_at,
        routing_account_id=account_id,
    )


def test_real_format_html_login_otp_pipeline_ignores_css_six_digit_values(core):
    account_id, rental_id = _active_rental(core)
    message = _fixture_raw("pixstorm_login_otp.sanitized.eml", "real-login", account_id)
    events, secrets, watcher, _ = _services(core, [message])
    published = watcher.poll_once(NOW, NOW)
    assert published[0].message_type == EmailMessageType.LOGIN_OTP
    assert OTPService(events, secrets, 120, 0).request_otp(rental_id, "buyer", NOW, NOW) == "654321"


def test_real_format_html_password_change_never_enters_otp_path(core):
    account_id, rental_id = _active_rental(core)
    with core[0].db.session() as session, session.begin():
        operation = OperationRow(
            kind=OperationKind.ROTATE_PASSWORD,
            idempotency_key="real-format-rotation",
            status=OperationStatus.RUNNING,
            account_id=account_id,
            rental_id=rental_id,
            order_id=None,
            correlation_id="real-format-rotation",
            created_at=NOW,
            started_at=NOW,
        )
        session.add(operation)
        session.flush()
        operation_id = operation.id
    message = _fixture_raw("pixstorm_password_change.sanitized.eml", "real-password", account_id)
    events, secrets, watcher, _ = _services(core, [message])
    assert watcher.poll_once(NOW, NOW)[0].message_type == EmailMessageType.PASSWORD_CHANGE
    assert OTPService(events, secrets, 120, 0).request_otp(rental_id, "buyer", NOW, NOW) is None
    assert PasswordRotator(events, secrets).consume_expected_reset_url(operation_id, NOW) == (
        "https://login.pixstorm.ru/ru/sso/changePassword/000000?token=SANITIZED_RESET_TOKEN"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://login.pixstorm.ru/ru/sso/changePassword/000000?token=token",
        "https://evil.example/ru/sso/changePassword/000000?token=token",
        "https://login.pixstorm.ru.evil.example/ru/sso/changePassword/000000?token=token",
        "https://evil.example/?next=changePassword",
        "https://login.pixstorm.ru/ru/sso/changePassword/000000",
        "https://login.pixstorm.ru/ru/sso/changePassword/000000?token=",
        "https://login.pixstorm.ru/ru/sso/changePassword/000000?token=a&token=b",
        "https://login.pixstorm.ru/ru/sso/changePassword/000000?token=a&other=b",
        "https://login.pixstorm.ru/ru/sso/changePassword/000000?token=a#fragment",
        "https://user@login.pixstorm.ru/ru/sso/changePassword/000000?token=a",
        "https://login.pixstorm.ru:444/ru/sso/changePassword/000000?token=a",
        "https://login.pixstorm.ru/ru/sso/changePassword/000000/extra?token=a",
        "https://login.pixstorm.ru/ru/sso/changePassword/a/b/c?token=a",
        "https://login.pixstorm.ru/ru/sso/changePassword/?token=a",
        "https://login.pixstorm.ru/ru/sso/changePassword?token=a",
        "not-a-url",
    ],
)
def test_password_reset_url_requires_exact_https_host_and_path(url):
    message = RawEmail(
        "negative-url",
        "login@pixstorm.ru",
        "Подтверждение смены пароля для учетной записи",
        NOW,
        f"type=verify_password_change {url}",
    )
    assert EmailClassifier().classify(message).message_type == EmailMessageType.UNKNOWN


def test_stale_password_change_is_not_correlated_to_new_rotation(core):
    account_id, rental_id = _active_rental(core)
    with core[0].db.session() as session, session.begin():
        operation = OperationRow(
            kind=OperationKind.ROTATE_PASSWORD,
            idempotency_key="fresh-rotation",
            status=OperationStatus.RUNNING,
            account_id=account_id,
            rental_id=rental_id,
            order_id=None,
            correlation_id="fresh-rotation",
            created_at=NOW,
            started_at=NOW,
        )
        session.add(operation)
        session.flush()
        operation_id = operation.id
    old_message = _fixture_raw(
        "pixstorm_password_change.sanitized.eml", "stale-password", account_id, NOW - timedelta(days=1)
    )
    events, secrets, watcher, _ = _services(core, [old_message])
    watcher.poll_once(NOW - timedelta(days=2), NOW)
    assert events.get_event("stale-password").correlation_operation_id is None
    assert PasswordRotator(events, secrets).consume_expected_reset_url(operation_id, NOW) is None


def test_security_event_dispatch_recovers_after_ingestion_crash(core):
    account_id, rental_id = _active_rental(core)
    message = _fixture_raw("pixstorm_password_change.sanitized.eml", "crash-security", account_id)
    events, _, watcher, dispatcher = _services(core, [message])
    watcher.poll_once(NOW, NOW)  # Simulated crash before the independent dispatcher runs.
    assert events.get_event("crash-security").security_processing_state == "PENDING"

    assert dispatcher.dispatch_pending(NOW) == 1
    assert core[0].get_account(account_id).status == AccountStatus.SECURITY_ALERT
    assert core[0].get_rental(rental_id).status == RentalStatus.SECURITY_TERMINATED
    assert dispatcher.dispatch_pending(NOW) == 0
    with core[0].db.session() as session:
        assert session.scalar(select(func.count()).select_from(SecurityEventRow)) == 1
        assert session.scalar(
            select(ClassifiedEmailEventRow.security_processing_state).where(
                ClassifiedEmailEventRow.gmail_message_id == "crash-security"
            )
        ) == "PROCESSED"
