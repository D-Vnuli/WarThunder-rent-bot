"""Production-safe hosting, preflight, database, and operator primitives.

No class in this module creates a live network client.  Real transports remain
an explicitly injected deployment concern; dry-run composition uses blocked
offline boundaries and therefore cannot report a simulated mutation as success.
"""

import json
import logging
import os
import shutil
import signal
import sqlite3
import sys
import tempfile
import threading
import zipfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import BinaryIO, cast

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.adapters.fake import (
    FakeFunPayAdapter,
    FakeGmailAdapter,
    FakeOwnerNotifier,
    FakePixelStormAdapter,
)
from app.adapters.production_stores import (
    DeterministicTestProtector,
    DPAPISecretProtector,
    ProductionEphemeralEmailSecretStore,
    ProductionSecureStore,
    ProductionWebSessionStore,
    SecretProtector,
)
from app.application.lease_guard import SystemClock
from app.config.settings import Settings
from app.domain.funpay import FunPayHealth, MessageReceipt
from app.domain.models import LotOperationResult
from app.domain.pixelstorm import (
    PixelStormAuthResult,
    PixelStormCredentialResult,
    PixelStormHealth,
    PixelStormPasswordChangeResult,
    PixelStormRevocationResult,
)
from app.domain.ports import (
    EphemeralEmailSecretStore,
    FunPayPort,
    GmailPort,
    OwnerNotifier,
    PixelStormSecurityPort,
    SecureStorePort,
)
from app.main import create_application
from app.persistence.database import Database

APP_VERSION = "0.1.0-phase6"


class CheckState(StrEnum):
    READY = "READY"
    NOT_READY = "NOT_READY"
    DEGRADED = "DEGRADED"


@dataclass(frozen=True)
class HealthCheck:
    name: str
    state: CheckState
    reason: str = "READY"


class ProductionStartupError(RuntimeError):
    pass


class AlreadyRunningError(ProductionStartupError):
    pass


@dataclass(frozen=True)
class ProductionDependencies:
    funpay: FunPayPort
    gmail: object
    pixelstorm: PixelStormSecurityPort
    secure_store: object
    web_sessions: object
    ephemeral_secrets: object
    owner_notifier: object


class ProductionAdapterFactory:
    """Explicit deployment/injection boundary; it never substitutes Fake adapters."""

    def __init__(self, dependencies: ProductionDependencies | None = None) -> None:
        self._dependencies = dependencies

    def create(self, settings: Settings) -> ProductionDependencies:
        del settings
        if self._dependencies is None:
            raise ProductionStartupError("PRODUCTION_TRANSPORT_NOT_CONFIGURED")
        return self._dependencies


@dataclass
class ExternalHealth:
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    last_failure_category: str | None = None


class PollingBackoff:
    """Deterministic bounded exponential retry policy for pollers only."""

    def __init__(self, base_seconds: float, cap_seconds: float = 60.0) -> None:
        self._base = base_seconds
        self._cap = cap_seconds
        self._failures = 0

    def failure_delay(self) -> float:
        self._failures += 1
        return min(self._cap, self._base * (2 ** (self._failures - 1)))

    def success(self) -> None:
        self._failures = 0


class SupervisedTask:
    """Bounded task runner; failure is surfaced in readiness, never swallowed."""

    def __init__(self, name: str, base_backoff_seconds: float = 1.0) -> None:
        self.name = name
        self.health = ExternalHealth()
        self.backoff = PollingBackoff(base_backoff_seconds)
        self.critical_failure: str | None = None

    def run(self, now: datetime, action) -> float:
        try:
            action()
        except Exception as error:
            self.health.consecutive_failures += 1
            self.health.last_failure_category = type(error).__name__
            self.critical_failure = type(error).__name__
            return self.backoff.failure_delay()
        self.health.last_success_at = now
        self.health.consecutive_failures = 0
        self.health.last_failure_category = None
        self.critical_failure = None
        self.backoff.success()
        return 0.0


class CooldownOwnerNotifier:
    """Failure-isolated notification boundary with deterministic category cooldown."""

    sandbox_safe = True
    production_safe = True

    def __init__(self, delegate, cooldown_seconds: int) -> None:
        self._delegate = delegate
        self._cooldown = timedelta(seconds=cooldown_seconds)
        self._sent: dict[str, datetime] = {}

    def notify(self, notification) -> None:
        previous = self._sent.get(notification.category)
        if previous is not None and notification.occurred_at - previous < self._cooldown:
            return
        try:
            self._delegate.notify(notification)
        except Exception:
            # Notifications are operationally useful but never weaken fail-closed business logic.
            return
        self._sent[notification.category] = notification.occurred_at

    def recovered(self, category: str) -> None:
        self._sent.pop(category, None)


class SecretRedactor:
    sensitive_keys = {"password", "token", "secret", "cookie", "session", "authorization", "otp"}

    def __init__(self, settings: Settings) -> None:
        self._secrets = tuple(
            value
            for value in (
                settings.funpay_session,
                settings.gmail_refresh_token,
                settings.gmail_oauth_client_secret,
                settings.pixelstorm_password,
                settings.owner_notifier_token,
                settings.golden_key,
            )
            if value
        )

    def redact(self, value: object) -> object:
        if isinstance(value, dict):
            return {
                key: "[REDACTED]" if key.lower() in self.sensitive_keys else self.redact(item)
                for key, item in value.items()
            }
        rendered = str(value)
        for secret in self._secrets:
            rendered = rendered.replace(secret, "[REDACTED]")
        return rendered


class SafeJsonFormatter(logging.Formatter):
    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__()
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "component": record.name,
            # Never serialize an arbitrary log message: exception/message text can
            # contain a transport secret even when callers forget to classify it.
            "event_category": getattr(record, "event_category", "LOG_EVENT"),
            "result": getattr(record, "result", "UNKNOWN"),
        }
        return json.dumps(self._redactor.redact(payload), separators=(",", ":"), ensure_ascii=False)


def configure_safe_logging(settings: Settings) -> logging.Logger:
    logger = logging.getLogger("war_thunder_rent_bot")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    formatter = SafeJsonFormatter(SecretRedactor(settings))
    try:
        settings.log_path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            settings.log_path, maxBytes=1_000_000, backupCount=settings.log_retention, encoding="utf-8"
        )
    except OSError as error:
        handler = logging.StreamHandler()
        logger.addHandler(handler)
        handler.setFormatter(formatter)
        logger.error("LOG_PATH_UNAVAILABLE", extra={"event_category": "LOG_PATH_UNAVAILABLE", "result": type(error).__name__})
        return logger
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


class SingleInstanceGuard:
    """OS-backed machine-local lock; it is never a substitute for DB leases."""

    def __init__(self, runtime_dir: Path) -> None:
        self._path = runtime_dir / "runtime.lock"
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        self._handle = handle
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
        except OSError as error:
            handle.close()
            self._handle = None
            raise AlreadyRunningError("ALREADY_RUNNING") from error

    def close(self) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
        finally:
            self._handle.close()
            self._handle = None


def _alembic_config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.attributes["database_url"] = database_url
    return config


def expected_revision(database_url: str) -> str:
    script = ScriptDirectory.from_config(_alembic_config(database_url))
    return script.get_current_head() or ""


def current_revision(database: Database) -> str | None:
    with database.engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def migration_check(database: Database, database_url: str) -> HealthCheck:
    try:
        current = current_revision(database)
        expected = expected_revision(database_url)
    except Exception:
        return HealthCheck("MIGRATIONS", CheckState.NOT_READY, "DATABASE_MIGRATION_REQUIRED")
    return HealthCheck(
        "MIGRATIONS",
        CheckState.READY if current == expected else CheckState.NOT_READY,
        "READY" if current == expected else "DATABASE_MIGRATION_REQUIRED",
    )


def database_check(database: Database) -> HealthCheck:
    try:
        with database.engine.connect() as connection:
            integrity = connection.execute(text("PRAGMA integrity_check")).scalar_one()
            foreign_keys = list(connection.execute(text("PRAGMA foreign_key_check")))
    except Exception:
        return HealthCheck("DATABASE", CheckState.NOT_READY, "DATABASE_INTEGRITY_FAILED")
    if integrity != "ok" or foreign_keys:
        return HealthCheck("DATABASE", CheckState.NOT_READY, "DATABASE_INTEGRITY_FAILED")
    return HealthCheck("DATABASE", CheckState.READY)


def vault_check(path: Path, name: str) -> HealthCheck:
    """Check readability only; vault contents are never returned or logged."""
    if not path.parent.exists():
        return HealthCheck(name, CheckState.NOT_READY, f"{name}_UNAVAILABLE")
    if not path.exists():
        return HealthCheck(name, CheckState.DEGRADED, "NOT_CONFIGURED")
    try:
        path.read_bytes()
    except OSError:
        return HealthCheck(name, CheckState.NOT_READY, f"{name}_UNREADABLE")
    return HealthCheck(name, CheckState.READY)


def sqlite_backup(database_url: str, backup_dir: Path, retention: int) -> Path:
    source = _sqlite_path(database_url)
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"business-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}.sqlite3"
    source_connection = sqlite3.connect(source)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    backups = sorted(backup_dir.glob("business-*.sqlite3"), key=lambda item: item.stat().st_mtime)
    for old in backups[:-retention]:
        old.unlink()
    return target


def validate_backup(path: Path, database_url: str) -> HealthCheck:
    if not path.is_file():
        return HealthCheck("BACKUP", CheckState.NOT_READY, "BACKUP_NOT_FOUND")
    with tempfile.TemporaryDirectory(prefix="wt-backup-check-") as temporary:
        restored = Path(temporary) / "restored.sqlite3"
        shutil.copyfile(path, restored)
        connection = sqlite3.connect(restored)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        except sqlite3.DatabaseError:
            return HealthCheck("BACKUP", CheckState.NOT_READY, "BACKUP_INVALID")
        finally:
            connection.close()
        if integrity != ("ok",) or foreign_keys:
            return HealthCheck("BACKUP", CheckState.NOT_READY, "BACKUP_INVALID")
        database = Database(f"sqlite:///{restored.as_posix()}")
        try:
            result = database_check(database)
            if result.state != CheckState.READY:
                return HealthCheck("BACKUP", CheckState.NOT_READY, "BACKUP_INVALID")
            revision = migration_check(database, database_url)
            return HealthCheck("BACKUP", CheckState.READY if revision.state == CheckState.READY else CheckState.NOT_READY, "READY" if revision.state == CheckState.READY else "BACKUP_MIGRATION_MISMATCH")
        finally:
            database.engine.dispose()


def _sqlite_path(database_url: str) -> Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError("only SQLite business databases are supported")
    return Path(database_url.removeprefix(prefix))


class DryRunFunPayAdapter(FakeFunPayAdapter):
    """Offline boundary: no dry-run call can impersonate a live side effect."""

    def __init__(self) -> None:
        super().__init__()
        self.set_health(FunPayHealth.UNAVAILABLE)

    def send_message(self, buyer_id: str, text: str, *, idempotency_key: str, now: datetime) -> MessageReceipt:
        del text
        return MessageReceipt(idempotency_key, buyer_id, None, False, False, False, now, "DRY_RUN_BLOCKED")

    def disable_lots(self, account_id: str, external_lot_ids: list[str]) -> LotOperationResult:
        del account_id
        return LotOperationResult(len(external_lot_ids), 0, False, tuple(external_lot_ids), safe_error_category="DRY_RUN_BLOCKED")

    def enable_lots(self, account_id: str, external_lot_ids: list[str]) -> LotOperationResult:
        del account_id
        return LotOperationResult(len(external_lot_ids), 0, False, tuple(external_lot_ids), safe_error_category="DRY_RUN_BLOCKED")


class DryRunPixelStormAdapter(FakePixelStormAdapter):
    def health(self, account_id: str) -> PixelStormHealth:
        del account_id
        return PixelStormHealth.UNAVAILABLE

    def authenticate(self, account_id: str, login: str, password: str, *, otp: str | None = None) -> PixelStormAuthResult:
        del account_id, login, password, otp
        return PixelStormAuthResult.UNAVAILABLE

    def verify_credentials(self, account_id: str, login: str, password: str) -> PixelStormCredentialResult:
        del account_id, login, password
        return PixelStormCredentialResult.UNAVAILABLE

    def revoke_sessions(self, account_id: str) -> PixelStormRevocationResult:
        del account_id
        return PixelStormRevocationResult.AMBIGUOUS

    def change_password(self, account_id: str, login: str, current_password: str, pending_password: str) -> PixelStormPasswordChangeResult:
        del account_id, login, current_password, pending_password
        return PixelStormPasswordChangeResult.UNAVAILABLE

    def request_password_change(
        self, account_id: str, login: str, current_password: str
    ) -> PixelStormPasswordChangeResult:
        del account_id, login, current_password
        return PixelStormPasswordChangeResult.UNAVAILABLE

    def complete_password_change(
        self, account_id: str, reset_url: str, pending_password: str
    ) -> PixelStormPasswordChangeResult:
        del account_id, reset_url, pending_password
        return PixelStormPasswordChangeResult.UNAVAILABLE


def preflight(settings: Settings, database: Database, secure_store: object | None = None, web_sessions: object | None = None) -> list[HealthCheck]:
    account_check, lots_check = _account_preflight(database, secure_store)
    secure_check = _store_preflight("SECURE_STORE", secure_store, settings.secure_store_path)
    session_check = _store_preflight("WEB_SESSION_STORE", web_sessions, settings.web_session_store_path)
    checks = [
        HealthCheck("CONFIG", CheckState.READY),
        database_check(database),
        migration_check(database, settings.database_url),
        secure_check,
        session_check,
        HealthCheck("FUNPAY_CONFIGURATION", CheckState.DEGRADED if not settings.funpay_session else CheckState.READY, "DRY_RUN_BLOCKED" if settings.dry_run else "READY"),
        HealthCheck("GMAIL_CONFIGURATION", CheckState.DEGRADED if not settings.gmail_refresh_token else CheckState.READY, "DRY_RUN_BLOCKED" if settings.dry_run else "READY"),
        HealthCheck("PIXELSTORM_CONFIGURATION", CheckState.DEGRADED if settings.dry_run else CheckState.READY, "DRY_RUN_BLOCKED" if settings.dry_run else "READY"),
        account_check,
        lots_check,
        HealthCheck("FILESYSTEM", CheckState.READY if settings.runtime_dir.exists() else CheckState.NOT_READY, "READY" if settings.runtime_dir.exists() else "RUNTIME_DIRECTORY_MISSING"),
        HealthCheck("LOGGING", CheckState.READY),
        HealthCheck("BACKUP_TARGET", CheckState.READY if settings.backup_dir.exists() else CheckState.NOT_READY, "READY" if settings.backup_dir.exists() else "BACKUP_DIRECTORY_MISSING"),
        HealthCheck("CLOCK", CheckState.READY if SystemClock().now().tzinfo is UTC else CheckState.NOT_READY, "READY" if SystemClock().now().tzinfo is UTC else "CLOCK_NOT_UTC"),
        HealthCheck("RUNTIME_MODE", CheckState.READY),
    ]
    return checks


def _store_preflight(name: str, store: object | None, path: Path) -> HealthCheck:
    if store is not None:
        validate = getattr(store, "validate", None)
        if callable(validate):
            try:
                validate()
            except Exception:
                return HealthCheck(name, CheckState.NOT_READY, f"{name}_CORRUPT")
            return HealthCheck(name, CheckState.READY)
    return vault_check(path, name)


def _account_preflight(database: Database, secure_store: object | None) -> tuple[HealthCheck, HealthCheck]:
    try:
        with database.engine.connect() as connection:
            account = connection.execute(text("SELECT id,status FROM accounts LIMIT 1")).first()
    except Exception:
        return HealthCheck("ACCOUNT", CheckState.NOT_READY, "ACCOUNT_STATE_UNAVAILABLE"), HealthCheck("ACCOUNT_LOTS", CheckState.NOT_READY, "ACCOUNT_LOTS_UNAVAILABLE")
    if account is None:
        return HealthCheck("ACCOUNT", CheckState.NOT_READY, "ACCOUNT_NOT_CONFIGURED"), HealthCheck("ACCOUNT_LOTS", CheckState.NOT_READY, "ACCOUNT_LOTS_NOT_CONFIGURED")
    account_id, status = account
    try:
        with database.engine.connect() as connection:
            lot_count = connection.execute(
                text("SELECT count(*) FROM account_lots WHERE account_id=:account_id"), {"account_id": account_id}
            ).scalar_one()
    except Exception:
        return HealthCheck("ACCOUNT", CheckState.NOT_READY, "ACCOUNT_STATE_UNAVAILABLE"), HealthCheck("ACCOUNT_LOTS", CheckState.NOT_READY, "ACCOUNT_LOTS_UNAVAILABLE")
    lots = HealthCheck("ACCOUNT_LOTS", CheckState.READY if lot_count else CheckState.NOT_READY, "READY" if lot_count else "ACCOUNT_LOTS_NOT_CONFIGURED")
    if status != "AVAILABLE":
        return HealthCheck("ACCOUNT", CheckState.DEGRADED, f"ACCOUNT_{status}"), lots
    getter = getattr(secure_store, "get_current_credentials", None)
    if not callable(getter) or getter(account_id) is None:
        return HealthCheck("ACCOUNT", CheckState.NOT_READY, "ACCOUNT_CREDENTIALS_MISSING"), lots
    return HealthCheck("ACCOUNT", CheckState.READY), lots


class ProductionRuntime:
    def __init__(self, settings: Settings, database: Database, application, lock: SingleInstanceGuard, secure_store: object, web_sessions: object, ephemeral_secrets: object) -> None:
        self.settings = settings
        self.database = database
        self.application = application
        self._lock = lock
        self.secure_store = secure_store
        self.web_sessions = web_sessions
        self.ephemeral_secrets = ephemeral_secrets
        self.accepting_work = False
        self.closed = False
        self.health: dict[str, HealthCheck] = {}
        self._started = False
        self._maintenance_at: datetime | None = None
        self._cycle = SupervisedTask("runtime-cycle")

    def start(self) -> list[HealthCheck]:
        self._require_python()
        configure_safe_logging(self.settings)
        self._lock.acquire()
        try:
            checks = preflight(self.settings, self.database, self.secure_store, self.web_sessions)
            self.health = {check.name: check for check in checks}
            if any(check.state == CheckState.NOT_READY for check in checks):
                raise ProductionStartupError(next(check.reason for check in checks if check.state == CheckState.NOT_READY))
            checks.extend(consistency_checks(self.database))
            checks.append(lot_drift_check(self.database, self.application.manager.funpay))
            self.health = {check.name: check for check in checks}
            if any(check.state == CheckState.NOT_READY for check in checks):
                raise ProductionStartupError(next(check.reason for check in checks if check.state == CheckState.NOT_READY))
            self.application.reconcile_startup(SystemClock().now())
            self.accepting_work = (
                not self.settings.dry_run
                and self.health["ACCOUNT"].state == CheckState.READY
                and self.health["ACCOUNT_LOTS"].state == CheckState.READY
                and self.health["LOT_DRIFT"].state == CheckState.READY
            )
            self._started = True
            return checks
        except Exception:
            self._lock.close()
            raise

    def shutdown(self) -> None:
        if self.closed:
            return
        self.accepting_work = False
        self.application.close()
        self._lock.close()
        self.closed = True

    def _run_maintenance(self, now: datetime) -> None:
        if self._maintenance_at is not None and now - self._maintenance_at < timedelta(minutes=1):
            return
        purge = getattr(self.ephemeral_secrets, "purge_expired", None)
        if callable(purge):
            purge(now)
        cleanup_metadata(self.database, now, self.settings.cleanup_retention_days)
        self._maintenance_at = now

    def request_shutdown(self, *_: object) -> None:
        """Signal-safe entry point: it only begins the established shutdown order."""
        self.shutdown()

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.request_shutdown)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, self.request_shutdown)

    def run_once(self, now: datetime) -> float:
        if self.closed:
            raise ProductionStartupError("RUNTIME_STOPPED")
        if not self._started:
            raise ProductionStartupError("RUNTIME_NOT_STARTED")
        delay = self._cycle.run(now, lambda: self.application.run_once(now))
        state = CheckState.READY if delay == 0 else CheckState.DEGRADED
        reason = "READY" if delay == 0 else self._cycle.health.last_failure_category or "RUNTIME_FAILURE"
        self.health["RUNTIME_CYCLE"] = HealthCheck("RUNTIME_CYCLE", state, reason)
        if delay == 0:
            self._run_maintenance(now)
        return delay

    def run_forever(self, stop_event: threading.Event | None = None, *, max_cycles: int | None = None) -> None:
        """Service host that repeatedly executes the established bounded runtime."""
        if not self._started:
            self.start()
        stopper = stop_event or threading.Event()
        cycles = 0
        while not self.closed and not stopper.is_set():
            delay = self.run_once(SystemClock().now())
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            stopper.wait(max(0.01, delay or 0.1))

    @staticmethod
    def _require_python() -> None:
        if sys.version_info < (3, 12):  # noqa: UP036
            raise ProductionStartupError("PYTHON_3_12_REQUIRED")


def create_production_application(
    settings: Settings,
    *,
    database: Database | None = None,
    adapter_factory: ProductionAdapterFactory | None = None,
    protector: SecretProtector | None = None,
) -> ProductionRuntime:
    settings.require_safe_mode()
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    settings.secure_store_path.parent.mkdir(parents=True, exist_ok=True)
    settings.web_session_store_path.parent.mkdir(parents=True, exist_ok=True)
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    runtime_database = database or Database(settings.database_url)
    sessions: object
    if settings.app_mode == "PRODUCTION":
        dependencies = (adapter_factory or ProductionAdapterFactory()).create(settings)
        funpay = dependencies.funpay
        pixelstorm = dependencies.pixelstorm
        secrets = dependencies.secure_store
        gmail = dependencies.gmail
        ephemeral = dependencies.ephemeral_secrets
        notifier = dependencies.owner_notifier
    else:
        runtime_protector = protector or (DPAPISecretProtector() if os.name == "nt" else DeterministicTestProtector())
        secrets = ProductionSecureStore(settings.secure_store_path, runtime_protector)
        # Construct the durable browser-session boundary even in dry run. It is
        # deliberately not handed to a live browser unless one is injected.
        sessions = ProductionWebSessionStore(settings.web_session_store_path, runtime_protector)
        ephemeral = ProductionEphemeralEmailSecretStore(
            settings.runtime_dir / "ephemeral-email.vault", runtime_protector
        )
        funpay = cast(FunPayPort, DryRunFunPayAdapter())
        pixelstorm = DryRunPixelStormAdapter()
        gmail = FakeGmailAdapter()
        notifier = CooldownOwnerNotifier(
            FakeOwnerNotifier(), settings.owner_notification_cooldown_seconds
        )
    application = create_application(
        settings=settings,
        database=runtime_database,
        funpay=cast(FunPayPort, funpay),
        pixelstorm=cast(PixelStormSecurityPort, pixelstorm),
        secrets=cast(SecureStorePort, secrets),
        gmail=cast(GmailPort, gmail),
        email_secrets=cast(EphemeralEmailSecretStore, ephemeral),
        owner_notifier=cast(OwnerNotifier, notifier),
        clock=SystemClock(),
        lease_heartbeat_interval_seconds=settings.lease_heartbeat_interval_seconds,
        start_reconciliation=False,
    )
    runtime_sessions = dependencies.web_sessions if settings.app_mode == "PRODUCTION" else sessions
    return ProductionRuntime(settings, runtime_database, application, SingleInstanceGuard(settings.runtime_dir), secrets, runtime_sessions, ephemeral)


def safe_status(settings: Settings, database: Database, checks: Iterable[HealthCheck]) -> dict[str, object]:
    with database.engine.connect() as connection:
        pending = connection.execute(text("SELECT count(*) FROM operations WHERE status='PENDING'")).scalar_one()
        running = connection.execute(text("SELECT count(*) FROM operations WHERE status='RUNNING'")).scalar_one()
        waiting = connection.execute(text("SELECT count(*) FROM operations WHERE security_state LIKE 'WAITING_%'")).scalar_one()
        lots = list(connection.execute(text("SELECT account_id, enabled_expected, count(*) FROM account_lots GROUP BY account_id, enabled_expected")))
        accounts = list(connection.execute(text("SELECT id,status,credential_version FROM accounts")))
        rentals = list(
            connection.execute(
                text("SELECT id,account_id,expires_at,status FROM rentals WHERE status='ACTIVE'")
            )
        )
        manual_review = list(
            connection.execute(
                text("SELECT id,kind,account_id,rental_id,security_state FROM operations WHERE status='FAILED'")
            )
        )
    latest_backup = max(settings.backup_dir.glob("business-*.sqlite3"), default=None, key=lambda item: item.stat().st_mtime)
    return {
        "application_version": APP_VERSION,
        "runtime_mode": settings.app_mode,
        "database_revision": current_revision(database),
        "pending_operations": pending,
        "running_operations": running,
        "waiting_operations": waiting,
        "stuck_operations": stuck_operations(database, datetime.now(UTC)),
        "lots": [{"account_id": row[0], "enabled": bool(row[1]), "count": row[2]} for row in lots],
        "last_backup": latest_backup.name if latest_backup is not None else None,
        "accounts": [{"account_id": row[0], "status": row[1], "credential_version": row[2]} for row in accounts],
        "active_rentals": [
            {"rental_id": row[0], "account_id": row[1], "expires_at": row[2], "status": row[3]}
            for row in rentals
        ],
        "manual_review": [
            {"operation_id": row[0], "kind": row[1], "account_id": row[2], "rental_id": row[3], "reason": row[4], "hint": "operator review required"}
            for row in manual_review
        ],
        "readiness": [asdict(check) for check in checks],
    }


def create_diagnostics(settings: Settings, database: Database, checks: Iterable[HealthCheck], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wt-diagnostics-") as temporary:
        root = Path(temporary)
        (root / "settings.json").write_text(json.dumps(settings.safe_summary(), indent=2), encoding="utf-8")
        (root / "status.json").write_text(json.dumps(safe_status(settings, database, checks), default=str, indent=2), encoding="utf-8")
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in root.iterdir():
                archive.write(item, item.name)
    return output


def stuck_operations(database: Database, now: datetime, threshold_seconds: int = 300) -> list[dict[str, object]]:
    threshold = now - timedelta(seconds=threshold_seconds)
    with database.engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id,kind,status,created_at,security_state FROM operations "
                "WHERE status IN ('PENDING','RUNNING') AND created_at < :threshold"
            ),
            {"threshold": threshold},
        )
    return [
        {"category": "STUCK_OPERATION", "operation_id": row[0], "kind": row[1], "state": row[2], "created_at": row[3], "security_state": row[4]}
        for row in rows
    ]


def cleanup_metadata(database: Database, now: datetime, retention_days: int) -> dict[str, int]:
    cutoff = now - timedelta(days=retention_days)
    with database.engine.begin() as connection:
        operations = connection.execute(
            text("DELETE FROM operations WHERE status='COMPLETED' AND completed_at < :cutoff"),
            {"cutoff": cutoff},
        ).rowcount or 0
        receipts = connection.execute(
            text("DELETE FROM message_receipts WHERE occurred_at < :cutoff"), {"cutoff": cutoff}
        ).rowcount or 0
        events = connection.execute(
            text("DELETE FROM funpay_events WHERE processing_status='COMPLETED' AND updated_at < :cutoff"),
            {"cutoff": cutoff},
        ).rowcount or 0
    return {"operations": operations, "message_receipts": receipts, "funpay_events": events}


def consistency_checks(database: Database) -> list[HealthCheck]:
    with database.engine.connect() as connection:
        duplicate_active = connection.execute(
            text("SELECT account_id FROM rentals WHERE status='ACTIVE' GROUP BY account_id HAVING count(*) > 1")
        ).first()
        account_without_rental = connection.execute(
            text("SELECT a.id FROM accounts a LEFT JOIN rentals r ON r.account_id=a.id AND r.status='ACTIVE' WHERE a.status='ACTIVE' AND r.id IS NULL")
        ).first()
        invalid_claim = connection.execute(
            text("SELECT id FROM operations WHERE status != 'RUNNING' AND (normal_claim_token IS NOT NULL OR recovery_claim_token IS NOT NULL)")
        ).first()
        rental_available = connection.execute(
            text("SELECT r.id FROM rentals r JOIN accounts a ON a.id=r.account_id WHERE r.status='ACTIVE' AND a.status='AVAILABLE'")
        ).first()
        incomplete_available = connection.execute(
            text("SELECT a.id FROM accounts a JOIN operations o ON o.account_id=a.id WHERE a.status='AVAILABLE' AND o.status IN ('PENDING','RUNNING') AND o.kind IN ('REVOKE_SESSIONS','ROTATE_PASSWORD')")
        ).first()
        conflicting_lifecycle = connection.execute(
            text("SELECT account_id FROM operations WHERE status IN ('PENDING','RUNNING') AND kind IN ('ENABLE_LOTS','DISABLE_LOTS','REVOKE_SESSIONS','ROTATE_PASSWORD') GROUP BY account_id HAVING count(*) > 1")
        ).first()
        waiting_without_time = connection.execute(
            text("SELECT id FROM operations WHERE security_state LIKE 'WAITING_%' AND maintenance_login_requested_at IS NULL AND password_change_requested_at IS NULL")
        ).first()
    failures = [item for item in (duplicate_active, account_without_rental, invalid_claim, rental_available, incomplete_available, conflicting_lifecycle, waiting_without_time) if item]
    return [
        HealthCheck("CONSISTENCY", CheckState.NOT_READY, "CONSISTENCY_CRITICAL")
        if failures
        else HealthCheck("CONSISTENCY", CheckState.READY)
    ]


def lot_drift_check(database: Database, funpay: FunPayPort) -> HealthCheck:
    """Read-only lot diagnostic; it never attempts an automatic remote repair."""
    with database.engine.connect() as connection:
        lots = list(
            connection.execute(
                text("SELECT a.status,l.external_lot_id,l.enabled_expected FROM accounts a JOIN account_lots l ON l.account_id=a.id")
            )
        )
    for status, lot_id, expected in lots:
        actual = funpay.get_lot_state(lot_id)
        if actual is None:
            continue
        if status != "AVAILABLE" and actual:
            return HealthCheck("LOT_DRIFT", CheckState.NOT_READY, "LOT_ON_FOR_NON_AVAILABLE_ACCOUNT")
        if status == "AVAILABLE" and (not actual or not expected):
            return HealthCheck("LOT_DRIFT", CheckState.DEGRADED, "LOT_OFF_FOR_AVAILABLE_ACCOUNT")
    return HealthCheck("LOT_DRIFT", CheckState.READY)
