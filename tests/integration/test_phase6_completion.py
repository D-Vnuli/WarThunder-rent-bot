"""Runtime-path acceptance coverage for the final PHASE 6 hardening contract.

Every transport in this file is an injected offline recording boundary.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread

import pytest
from sqlalchemy import text

from alembic import command
from app.adapters.production_notifier import TelegramOwnerNotifier
from app.adapters.production_stores import (
    DeterministicTestProtector,
    ProductionEphemeralEmailSecretStore,
    ProductionSecureStore,
    ProductionWebSessionStore,
    VaultCorruptError,
)
from app.config.settings import RuntimeMode, Settings
from app.domain.funpay import FunPayHealth, MessageReceipt
from app.domain.models import LotOperationResult
from app.domain.notifications import OwnerNotification
from app.domain.pixelstorm import (
    PixelStormAuthenticationState,
    PixelStormAuthResult,
    PixelStormCredentialResult,
    PixelStormHealth,
    PixelStormPasswordChangeResult,
    PixelStormRevocationResult,
    PixelStormSecurityCapabilities,
    PixelStormSessionState,
)
from app.persistence.database import Database
from app.persistence.repositories import Repository
from app.production import (
    CheckState,
    CooldownOwnerNotifier,
    ProductionAdapterFactory,
    ProductionDependencies,
    ProductionStartupError,
    _alembic_config,
    configure_safe_logging,
    consistency_checks,
    create_diagnostics,
    create_production_application,
    lot_drift_check,
    preflight,
    safe_status,
)


class RecordingFunPay:
    production_safe = True

    def __init__(self) -> None:
        self.states: dict[str, bool] = {}
        self.poll_failures = 0
        self.calls: list[str] = []

    def poll_events(self, *, after):
        del after
        self.calls.append("poll")
        if self.poll_failures:
            self.poll_failures -= 1
            raise RuntimeError("offline")
        return ()

    def get_order(self, funpay_order_id):
        del funpay_order_id
        return None

    def send_message(self, buyer_id, text, *, idempotency_key, now):
        del text
        self.calls.append("send")
        return MessageReceipt(idempotency_key, buyer_id, "recorded", True, True, False, now)

    def get_message_receipt(self, idempotency_key):
        del idempotency_key
        return None

    def get_lot_state(self, external_lot_id):
        return self.states.get(external_lot_id)

    def verify_lots_disabled(self, external_lot_ids):
        return LotOperationResult(len(external_lot_ids), len(external_lot_ids), True, ())

    def verify_lots_enabled(self, external_lot_ids):
        return LotOperationResult(len(external_lot_ids), len(external_lot_ids), True, ())

    def health(self):
        return FunPayHealth.READY

    def disable_lots(self, account_id, external_lot_ids):
        del account_id
        for lot in external_lot_ids:
            self.states[lot] = False
        return self.verify_lots_disabled(external_lot_ids)

    def enable_lots(self, account_id, external_lot_ids):
        del account_id
        for lot in external_lot_ids:
            self.states[lot] = True
        return self.verify_lots_enabled(external_lot_ids)


class RecordingPixelStorm:
    production_safe = True

    def health(self, account_id):
        del account_id
        return PixelStormHealth.UNAVAILABLE

    def inspect_authentication_state(self, account_id):
        return PixelStormAuthenticationState(self.health(account_id), False)

    def inspect_security_capabilities(self, account_id):
        del account_id
        return PixelStormSecurityCapabilities(False, False, False, False, unknown=True)

    def inspect_session_state(self, account_id):
        return PixelStormSessionState(self.health(account_id), False, False)

    def authenticate(self, account_id, login, password, *, otp=None):
        del account_id, login, password, otp
        return PixelStormAuthResult.UNAVAILABLE

    def verify_credentials(self, account_id, login, password):
        del account_id, login, password
        return PixelStormCredentialResult.UNAVAILABLE

    def revoke_sessions(self, account_id):
        del account_id
        return PixelStormRevocationResult.AMBIGUOUS

    def verify_revocation(self, account_id):
        del account_id
        return PixelStormRevocationResult.AMBIGUOUS

    def change_password(self, account_id, login, current_password, pending_password):
        del account_id, login, current_password, pending_password
        return PixelStormPasswordChangeResult.UNAVAILABLE

    def request_password_change(self, account_id, login, current_password):
        del account_id, login, current_password
        return PixelStormPasswordChangeResult.UNAVAILABLE

    def complete_password_change(self, account_id, reset_url, pending_password):
        del account_id, reset_url, pending_password
        return PixelStormPasswordChangeResult.UNAVAILABLE


class RecordingGmail:
    production_safe = True

    def get_new_messages(self, *, after):
        del after
        return ()


class RecordingOwnerTransport:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def send_owner_notification(self, category: str, safe_context: str) -> None:
        self.messages.append((category, safe_context))


def _settings(tmp_path: Path, *, mode: str = RuntimeMode.PRODUCTION) -> Settings:
    root = tmp_path / "runtime"
    return Settings(
        app_mode=mode,
        dry_run=mode != RuntimeMode.PRODUCTION,
        allow_live_operations=mode == RuntimeMode.PRODUCTION,
        allow_funpay_mutations=mode == RuntimeMode.PRODUCTION,
        allow_pixelstorm_security_mutations=mode == RuntimeMode.PRODUCTION,
        database_url=f"sqlite:///{(tmp_path / 'business.sqlite3').as_posix()}",
        runtime_dir=root,
        secure_store_path=root / "secure.vault",
        web_session_store_path=root / "sessions.vault",
        backup_dir=root / "backups",
        log_path=root / "logs" / "app.jsonl",
    )


def _migrate(settings: Settings) -> Database:
    command.upgrade(_alembic_config(settings.database_url), "head")
    return Database(settings.database_url)


def _dependencies(settings: Settings, funpay: RecordingFunPay | None = None) -> ProductionDependencies:
    protector = DeterministicTestProtector()
    owner = TelegramOwnerNotifier(RecordingOwnerTransport())
    return ProductionDependencies(
        funpay=funpay or RecordingFunPay(),
        gmail=RecordingGmail(),
        pixelstorm=RecordingPixelStorm(),
        secure_store=ProductionSecureStore(settings.secure_store_path, protector),
        web_sessions=ProductionWebSessionStore(settings.web_session_store_path, protector),
        ephemeral_secrets=ProductionEphemeralEmailSecretStore(settings.runtime_dir / "email.vault", protector),
        owner_notifier=CooldownOwnerNotifier(owner, 1),
    )


def _seed(runtime, status: str = "AVAILABLE") -> str:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    account_id = runtime.application.repository.add_account("production", now)
    runtime.application.repository.add_account_lot(account_id, "lot-production", now)
    runtime.secure_store.set_current_credentials(account_id, "login", "PHASE6_PIXELSTORM_PASSWORD_SECRET")
    if status != "AVAILABLE":
        with runtime.database.engine.begin() as connection:
            connection.execute(text("UPDATE accounts SET status=:status WHERE id=:id"), {"status": status, "id": account_id})
            if status == "ACTIVE":
                connection.execute(text("INSERT INTO orders(id,funpay_order_id,buyer_id,tariff_code,duration_seconds,account_id,fulfillment_status,received_at,safe_metadata) VALUES ('order-production','production-order','buyer','tariff',60,:id,'FULFILLED',:now,'{}')"), {"id": account_id, "now": now})
                connection.execute(text("INSERT INTO rentals(id,order_id,buyer_id,account_id,tariff_code,expires_at,status,credential_version,created_at,updated_at) VALUES ('rental-production','order-production','buyer',:id,'tariff',:expiry,'ACTIVE',1,:now,:now)"), {"id": account_id, "expiry": now + timedelta(minutes=1), "now": now})
    return account_id


def test_production_composition_is_explicit_and_never_falls_back_to_fake(tmp_path):
    settings = _settings(tmp_path)
    _migrate(settings)
    with pytest.raises(ProductionStartupError, match="PRODUCTION_TRANSPORT_NOT_CONFIGURED"):
        create_production_application(settings)
    dependencies = _dependencies(settings)
    runtime = create_production_application(settings, adapter_factory=ProductionAdapterFactory(dependencies))
    account_id = _seed(runtime)
    try:
        checks = runtime.start()
        assert runtime.application.manager.funpay is dependencies.funpay
        assert type(runtime.application.manager.funpay).__name__ != "FakeFunPayAdapter"
        assert type(runtime.secure_store).__name__ == "ProductionSecureStore"
        assert runtime.accepting_work is True
        assert all(item.state != CheckState.NOT_READY for item in checks)
        assert account_id
    finally:
        runtime.shutdown()


def test_production_persistent_stores_restart_validate_and_expire_without_plaintext(tmp_path):
    protector = DeterministicTestProtector(b"test-key")
    secure_path, sessions_path, email_path = tmp_path / "secure.vault", tmp_path / "sessions.vault", tmp_path / "email.vault"
    first = ProductionSecureStore(secure_path, protector)
    first.set_current_credentials("a", "login", "PHASE6_PIXELSTORM_PASSWORD_SECRET")
    first.set_pending_credentials("a", "login", "pending")
    sessions = ProductionWebSessionStore(sessions_path, protector)
    sessions.set_funpay_session("a", "PHASE6_FUNPAY_SESSION_SECRET")
    sessions.set_pixelstorm_session("a", "PHASE6_BROWSER_SESSION_SECRET")
    emails = ProductionEphemeralEmailSecretStore(email_path, protector)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert emails.put("e", "PHASE6_OTP_SECRET", expires_at=now + timedelta(seconds=1))
    restored = ProductionSecureStore(secure_path, protector)
    assert restored.get_current_credentials("a") == ("login", "PHASE6_PIXELSTORM_PASSWORD_SECRET")
    assert restored.get_pending_credentials("a") == ("login", "pending")
    assert ProductionWebSessionStore(sessions_path, protector).get_funpay_session("a") == "PHASE6_FUNPAY_SESSION_SECRET"
    restored_email = ProductionEphemeralEmailSecretStore(email_path, protector)
    assert restored_email.consume_once("e", claim_token="claim", now=now) == "PHASE6_OTP_SECRET"
    assert restored_email.consume_once("e", claim_token="claim", now=now) is None
    assert b"PHASE6_" not in secure_path.read_bytes() + sessions_path.read_bytes() + email_path.read_bytes()
    secure_path.write_bytes(b"garbage")
    with pytest.raises(VaultCorruptError):
        ProductionSecureStore(secure_path, protector).validate()


def test_preflight_account_modes_corrupt_vault_and_runtime_supervision(tmp_path):
    settings = _settings(tmp_path)
    database = _migrate(settings)
    dependencies = _dependencies(settings)
    assert {item.name: item.state for item in preflight(settings, database, dependencies.secure_store, dependencies.web_sessions)}["ACCOUNT"] == CheckState.NOT_READY
    runtime = create_production_application(settings, database=database, adapter_factory=ProductionAdapterFactory(dependencies))
    _seed(runtime, "ACTIVE")
    try:
        assert runtime.start() and runtime.accepting_work is False
        runtime.application.run_once = lambda now: (_ for _ in ()).throw(RuntimeError("poll down"))
        assert runtime.run_once(datetime(2026, 1, 2, tzinfo=UTC)) == 1
        assert runtime.health["RUNTIME_CYCLE"].state == CheckState.DEGRADED
        runtime.application.run_once = lambda now: None
        assert runtime.run_once(datetime(2026, 1, 2, tzinfo=UTC)) == 0
        assert runtime.health["RUNTIME_CYCLE"].state == CheckState.READY
    finally:
        runtime.shutdown()
    settings.secure_store_path.write_bytes(b"invalid")
    check = {item.name: item for item in preflight(settings, database, ProductionSecureStore(settings.secure_store_path, DeterministicTestProtector()), dependencies.web_sessions)}
    assert check["SECURE_STORE"].reason == "SECURE_STORE_CORRUPT"


def test_runtime_maintenance_status_notifier_and_lot_drift_are_integrated(tmp_path):
    settings = _settings(tmp_path)
    database = _migrate(settings)
    funpay = RecordingFunPay()
    dependencies = _dependencies(settings, funpay)
    runtime = create_production_application(settings, database=database, adapter_factory=ProductionAdapterFactory(dependencies))
    account_id = _seed(runtime)
    funpay.states["lot-production"] = True
    now = datetime(2026, 1, 1, tzinfo=UTC)
    runtime.ephemeral_secrets.put("expired", "PHASE6_RESET_SECRET", expires_at=now - timedelta(seconds=1))
    try:
        runtime.start()
        runtime.application.run_once = lambda now: None
        runtime.run_once(now)
        assert runtime.ephemeral_secrets.consume_once("expired", claim_token="x", now=now) is None
        status = safe_status(settings, database, runtime.health.values())
        assert {"lots", "waiting_operations", "stuck_operations", "last_backup", "readiness"} <= status.keys()
        with database.engine.begin() as connection:
            connection.execute(text("UPDATE accounts SET status='MANUAL_REVIEW' WHERE id=:id"), {"id": account_id})
        assert lot_drift_check(database, funpay).state == CheckState.NOT_READY
    finally:
        runtime.shutdown()
    transport = RecordingOwnerTransport()
    notifier = CooldownOwnerNotifier(TelegramOwnerNotifier(transport), 60)
    notifier.notify(OwnerNotification("AUTH_REQUIRED", "safe", now, safe_error_category="AUTH_REQUIRED"))
    assert transport.messages == [("AUTH_REQUIRED", "AUTH_REQUIRED")]


def test_runtime_service_shutdown_is_bounded_and_never_replays_blocked_cycle(tmp_path):
    settings = _settings(tmp_path)
    database = _migrate(settings)
    dependencies = _dependencies(settings)
    runtime = create_production_application(settings, database=database, adapter_factory=ProductionAdapterFactory(dependencies))
    _seed(runtime)
    entered, release, stopped = Event(), Event(), Event()

    def blocked(now):
        del now
        entered.set()
        release.wait(1)
        stopped.set()

    runtime.application.run_once = blocked
    try:
        runtime.start()
        worker = Thread(target=lambda: runtime.run_once(datetime(2026, 1, 1, tzinfo=UTC)))
        worker.start()
        assert entered.wait(1)
        runtime.request_shutdown()
        release.set()
        worker.join(1)
        assert stopped.is_set() and runtime.closed
        with pytest.raises(ProductionStartupError, match="RUNTIME_STOPPED"):
            runtime.run_once(datetime(2026, 1, 1, tzinfo=UTC))
    finally:
        release.set()
        runtime.shutdown()


def test_phase6_secret_scan_covers_safe_artifacts_and_logging(tmp_path):
    settings = _settings(tmp_path).model_copy(update={
        "funpay_session": "PHASE6_FUNPAY_SESSION_SECRET",
        "gmail_refresh_token": "PHASE6_GMAIL_REFRESH_SECRET",
        "pixelstorm_password": "PHASE6_PIXELSTORM_PASSWORD_SECRET",
        "owner_notifier_token": "PHASE6_OWNER_TOKEN_SECRET",
        "golden_key": "PHASE6_BROWSER_SESSION_SECRET",
    })
    database = _migrate(settings)
    logger = configure_safe_logging(settings)
    logger.error("PHASE6_OTP_SECRET PHASE6_RESET_SECRET")
    for handler in logger.handlers:
        handler.flush()
    diagnostics = create_diagnostics(settings, database, preflight(settings, database), tmp_path / "diagnostics.zip")
    import zipfile

    with zipfile.ZipFile(diagnostics) as archive:
        output = "".join(archive.read(name).decode("utf-8") for name in archive.namelist())
    output += settings.log_path.read_text(encoding="utf-8")
    assert "PHASE6_" not in output


def test_consistency_matrix_flags_dangerous_durable_state(tmp_path):
    settings = _settings(tmp_path)
    database = _migrate(settings)
    repository = Repository(database)
    account = repository.add_account("consistency", datetime(2026, 1, 1, tzinfo=UTC))
    with database.engine.begin() as connection:
        connection.execute(text("INSERT INTO operations(id,kind,idempotency_key,status,account_id,correlation_id,attempt_count,security_state,created_at,safe_metadata) VALUES ('wait','ROTATE_PASSWORD','wait','PENDING',:account,'wait',0,'WAITING_LOGIN_OTP',:now,'{}')"), {"account": account, "now": datetime(2026, 1, 1, tzinfo=UTC)})
    assert consistency_checks(database)[0].state == CheckState.NOT_READY
