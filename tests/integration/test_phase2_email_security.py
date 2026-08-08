from datetime import timedelta

from sqlalchemy import func, select

from app.adapters.email_classifier import EmailClassifier, EmailClassifierPolicy
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
    EmailPayloadState,
    OperationKind,
    OperationStatus,
    RentalStatus,
)
from app.persistence.classified_email_events import ClassifiedEmailRepository
from app.persistence.models import (
    AuditEventRow,
    ClassifiedEmailEventRow,
    OperationRow,
    SecurityEventRow,
)
from tests.helpers import create_test_account

SENDER = "login@pixstorm.ru"
PASSWORD_SUBJECT = "Подтверждение смены пароля для учетной записи"
OTP = "654321"
RESET_URL = "https://login.pixstorm.ru/ru/sso/changePassword/000000?token=SANITIZED_RESET_TOKEN"


def _watcher(core, messages, ttl=300):
    repository, _, _, _ = core
    events = ClassifiedEmailRepository(repository.db)
    secrets = FakeEphemeralEmailSecretStore()
    watcher = GmailWatcher(
        FakeGmailAdapter(messages),
        EmailClassifier(
            EmailClassifierPolicy(
                allowed_sender=SENDER,
                password_change_subject=PASSWORD_SUBJECT,
            )
        ),
        events,
        secrets,
        ttl,
        ttl,
    )
    return events, secrets, watcher, EmailEventDispatcher(events, SecurityMonitor(repository))


def _login(message_id, now, account_id=None, code=OTP):
    return RawEmail(
        message_id,
        SENDER,
        "Подтверждение входа",
        now,
        f"Код подтверждения для входа: {code}; type=two_step_email_code",
        account_id,
    )


def _password_change(message_id, now, account_id=None):
    return RawEmail(
        message_id,
        SENDER,
        PASSWORD_SUBJECT,
        now,
        f"type=verify_password_change {RESET_URL}",
        account_id,
    )


def _active_rental(core, now, duration=3600):
    repository, manager, funpay, _ = core
    account_id = create_test_account(repository, funpay, "WT01", now)
    result = manager.accept_order(OrderInput("order", "buyer", "1H", duration), now)
    manager.run_operations(now)
    manager.run_operations(now)
    assert result.rental_id is not None
    return account_id, result.rental_id


def test_valid_login_otp_is_classified_and_delivered_only_through_otp_service(core, now):
    account_id, rental_id = _active_rental(core, now)
    events, secrets, watcher, _ = _watcher(core, [_login("otp-1", now, account_id)])
    published = watcher.poll_once(now, now)
    assert published[0].message_type == EmailMessageType.LOGIN_OTP
    otp = OTPService(events, secrets, 120, 0).request_otp(rental_id, "buyer", now, now)
    assert otp == OTP


def test_password_change_never_becomes_otp_and_reset_url_never_reaches_otp_path(core, now):
    account_id, rental_id = _active_rental(core, now)
    events, secrets, watcher, _ = _watcher(core, [_password_change("password-1", now, account_id)])
    published = watcher.poll_once(now, now)
    assert published[0].message_type == EmailMessageType.PASSWORD_CHANGE
    assert OTPService(events, secrets, 120, 0).request_otp(rental_id, "buyer", now, now) is None
    assert RESET_URL not in "".join(
        row.safe_metadata
        for row in [events.get_event("password-1")]
        if row is not None
    )


def test_unknown_malformed_and_bare_six_digits_are_denied(core, now):
    account_id, rental_id = _active_rental(core, now)
    unknown = RawEmail("unknown", "other@example.invalid", "hello", now, f"{OTP}", account_id)
    malformed = RawEmail("malformed", SENDER, "Подтверждение входа", now, OTP, account_id)
    events, secrets, watcher, _ = _watcher(core, [unknown, malformed])
    published = watcher.poll_once(now, now)
    assert [event.message_type for event in published] == [EmailMessageType.UNKNOWN] * 2
    assert OTPService(events, secrets, 120, 0).request_otp(rental_id, "buyer", now, now) is None


def test_duplicate_gmail_message_is_ignored(core, now):
    account_id, _ = _active_rental(core, now)
    events, _, watcher, _ = _watcher(core, [_login("duplicate", now, account_id)])
    assert len(watcher.poll_once(now, now)) == 1
    assert watcher.poll_once(now, now) == []
    with events.db.session() as session:
        assert session.scalar(select(func.count()).select_from(ClassifiedEmailEventRow)) == 1


def test_otp_gating_rejects_stale_prestart_wrong_buyer_inactive_and_expired(core, now):
    account_id, rental_id = _active_rental(core, now, duration=30)
    events, secrets, watcher, _ = _watcher(
        core,
        [
            _login("stale", now - timedelta(seconds=121), account_id),
            _login("before-start", now - timedelta(seconds=1), account_id),
            _login("valid", now, account_id),
        ],
    )
    watcher.poll_once(now - timedelta(seconds=200), now)
    otp_service = OTPService(events, secrets, 120, 0)
    assert otp_service.request_otp(rental_id, "other", now, now) is None
    assert otp_service.request_otp(rental_id, "buyer", now, now + timedelta(seconds=31)) is None
    assert core[0].get_rental(rental_id).status == RentalStatus.ACTIVE
    assert core[0].get_account(account_id).status == AccountStatus.ACTIVE


def test_otp_rejects_stale_and_pre_rental_email_independently(core, now):
    account_id, rental_id = _active_rental(core, now)
    events, secrets, watcher, _ = _watcher(
        core,
        [
            _login("too-old", now - timedelta(seconds=121), account_id),
            _login("before-rental", now - timedelta(seconds=1), account_id),
        ],
    )
    watcher.poll_once(now - timedelta(seconds=200), now)
    service = OTPService(events, secrets, 120, 0)
    assert service.request_otp(rental_id, "buyer", now, now) is None


def test_otp_rejects_inactive_rental(core, now):
    account_id, rental_id = _active_rental(core, now)
    events, secrets, watcher, _ = _watcher(core, [_login("inactive", now, account_id)])
    watcher.poll_once(now, now)
    core[0].expire_due(now + timedelta(seconds=3601))
    assert OTPService(events, secrets, 120, 0).request_otp(
        rental_id, "buyer", now, now + timedelta(seconds=3601)
    ) is None


def test_otp_lookback_one_time_consume_and_expired_payload(core, now):
    account_id, rental_id = _active_rental(core, now)
    events, secrets, watcher, _ = _watcher(core, [_login("lookback", now, account_id)])
    watcher.poll_once(now, now)
    service = OTPService(events, secrets, 120, 0)
    assert service.request_otp(rental_id, "buyer", now + timedelta(seconds=30), now + timedelta(seconds=30)) == OTP
    assert service.request_otp(rental_id, "buyer", now + timedelta(seconds=31), now + timedelta(seconds=31)) is None

    events2, secrets2, watcher2, _ = _watcher(core, [_login("expired", now, account_id)], ttl=1)
    watcher2.poll_once(now, now)
    assert OTPService(events2, secrets2, 120, 0).request_otp(
        rental_id, "buyer", now + timedelta(seconds=40), now + timedelta(seconds=40)
    ) is None
    assert events2.get_event("expired").payload_state == EmailPayloadState.UNUSABLE_EXPIRED


def test_ephemeral_store_purges_and_one_time_consumes(now):
    secrets = FakeEphemeralEmailSecretStore()
    assert secrets.put("event", OTP, expires_at=now + timedelta(seconds=1))
    assert secrets.consume_once("event", claim_token="claim", now=now) == OTP
    assert secrets.consume_once("event", claim_token="claim", now=now) is None
    assert secrets.put("expired", OTP, expires_at=now)
    assert secrets.purge_expired(now) == 1


def test_unexpected_password_change_creates_critical_security_event(core, now):
    account_id, rental_id = _active_rental(core, now)
    _, _, watcher, dispatcher = _watcher(core, [_password_change("unexpected", now, account_id)])
    watcher.poll_once(now, now)
    assert dispatcher.dispatch_pending(now) == 1
    assert core[0].get_account(account_id).status == AccountStatus.SECURITY_ALERT
    assert core[0].get_rental(rental_id).status == RentalStatus.SECURITY_TERMINATED
    with core[0].db.session() as session:
        assert session.scalar(select(func.count()).select_from(SecurityEventRow)) == 1


def test_expected_password_change_is_correlated_to_rotation_operation(core, now):
    account_id, rental_id = _active_rental(core, now)
    with core[0].db.session() as session, session.begin():
        operation = OperationRow(
            kind=OperationKind.ROTATE_PASSWORD,
            idempotency_key="test-rotation",
            status=OperationStatus.RUNNING,
            account_id=account_id,
            rental_id=rental_id,
            order_id=None,
            correlation_id="test-rotation",
            created_at=now,
            started_at=now,
        )
        session.add(operation)
        session.flush()
        operation_id = operation.id
    events, secrets, watcher, _ = _watcher(core, [_password_change("expected", now, account_id)])
    watcher.poll_once(now, now)
    assert events.get_event("expected").correlation_operation_id == operation_id
    assert core[0].get_account(account_id).status == AccountStatus.ACTIVE
    assert PasswordRotator(events, secrets).consume_expected_reset_url(operation_id, now) == RESET_URL


def test_sensitive_payload_is_absent_from_sqlite_audit_and_safe_metadata(core, now):
    account_id, _ = _active_rental(core, now)
    _, _, watcher, _ = _watcher(core, [_login("safe-otp", now, account_id), _password_change("safe-reset", now, account_id)])
    watcher.poll_once(now, now)
    with core[0].db.session() as session:
        texts = [row.safe_metadata for row in session.scalars(select(ClassifiedEmailEventRow))]
        texts.extend(row.safe_metadata for row in session.scalars(select(AuditEventRow)))
    assert all(OTP not in text and RESET_URL not in text for text in texts)
    with core[0].db.engine.connect() as connection:
        durable_values = [
            str(value)
            for table in ("classified_email_events", "processed_messages", "otp_requests", "security_events")
            for row in connection.exec_driver_sql(f"SELECT * FROM {table}")
            for value in row
        ]
    assert OTP not in durable_values
    assert RESET_URL not in durable_values
