import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from alembic import command
from app.adapters.fake import FakeOwnerNotifier, FakeSecureStore
from app.adapters.pixelstorm import PixelStormBrowserSessionFactory
from app.config.settings import RuntimeMode, Settings
from app.domain.notifications import OwnerNotification
from app.main import run_application
from app.manage import main as manage
from app.persistence.database import Database
from app.persistence.repositories import Repository
from app.production import (
    AlreadyRunningError,
    CheckState,
    CooldownOwnerNotifier,
    ProductionStartupError,
    SingleInstanceGuard,
    SupervisedTask,
    _alembic_config,
    cleanup_metadata,
    configure_safe_logging,
    consistency_checks,
    create_diagnostics,
    create_production_application,
    database_check,
    preflight,
    sqlite_backup,
    stuck_operations,
    validate_backup,
    vault_check,
)


def _settings(tmp_path: Path) -> Settings:
    runtime = tmp_path / "runtime"
    database = tmp_path / "business.sqlite3"
    return Settings(
        app_mode=RuntimeMode.PRODUCTION_DRY_RUN,
        dry_run=True,
        database_url=f"sqlite:///{database.as_posix()}",
        runtime_dir=runtime,
        secure_store_path=runtime / "secure-store.vault",
        web_session_store_path=runtime / "web-session.vault",
        log_path=runtime / "logs" / "app.jsonl",
        backup_dir=runtime / "backups",
    )


def _migrate(settings: Settings) -> None:
    command.upgrade(_alembic_config(settings.database_url), "head")


def _seed_rentable(runtime, code: str = "phase6") -> str:
    account_id = runtime.application.repository.add_account(code, datetime(2026, 1, 1, tzinfo=UTC))
    runtime.application.repository.add_account_lot(account_id, f"{code}-lot", datetime(2026, 1, 1, tzinfo=UTC))
    runtime.secure_store.set_current_credentials(account_id, "login", "password")
    return account_id


def test_phase6_runtime_modes_gates_timing_and_safe_settings_repr(tmp_path):
    settings = _settings(tmp_path).model_copy(
        update={
            "funpay_session": "PHASE6_FUNPAY_SESSION_SECRET",
            "gmail_refresh_token": "PHASE6_GMAIL_REFRESH_SECRET",
            "pixelstorm_password": "PHASE6_PIXELSTORM_PASSWORD_SECRET",
        }
    )
    rendered = repr(settings) + json.dumps(settings.safe_summary())
    assert "PHASE6_" not in rendered
    with pytest.raises(RuntimeError, match="ALLOW_LIVE_OPERATIONS"):
        Settings(app_mode=RuntimeMode.PRODUCTION, dry_run=False).require_safe_mode()
    Settings(
        app_mode=RuntimeMode.PRODUCTION,
        dry_run=False,
        allow_live_operations=True,
    ).require_safe_mode()
    with pytest.raises(ValidationError, match="heartbeat"):
        Settings(normal_worker_lease_seconds=10, lease_heartbeat_interval_seconds=10)


def test_phase6_production_mutation_subgates_default_to_blocked(tmp_path):
    settings = _settings(tmp_path).model_copy(
        update={"app_mode": RuntimeMode.PRODUCTION, "dry_run": False, "allow_live_operations": True}
    )
    with pytest.raises(ProductionStartupError, match="PRODUCTION_TRANSPORT_NOT_CONFIGURED"):
        create_production_application(settings)


def test_phase6_production_dry_run_preflight_and_no_auto_migration(tmp_path):
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    checks = preflight(settings, database)
    assert next(item for item in checks if item.name == "MIGRATIONS").reason == "DATABASE_MIGRATION_REQUIRED"
    _migrate(settings)
    runtime = create_production_application(settings)
    try:
        _seed_rentable(runtime)
        checks = runtime.start()
        assert all(item.state != CheckState.NOT_READY for item in checks)
        assert runtime.accepting_work is False
    finally:
        runtime.shutdown()


def test_phase6_sqlite_integrity_backup_restore_and_retention(tmp_path):
    settings = _settings(tmp_path)
    _migrate(settings)
    database = Database(settings.database_url)
    assert database_check(database).state == CheckState.READY
    first = sqlite_backup(settings.database_url, settings.backup_dir, retention=1)
    second = sqlite_backup(settings.database_url, settings.backup_dir, retention=1)
    assert second.exists() and first != second
    assert len(list(settings.backup_dir.glob("business-*.sqlite3"))) == 1
    assert validate_backup(second, settings.database_url).state == CheckState.READY
    broken = tmp_path / "broken.sqlite3"
    broken.write_bytes(b"not sqlite")
    assert validate_backup(broken, settings.database_url).state == CheckState.NOT_READY


def test_phase6_single_instance_releases_on_shutdown(tmp_path):
    first, second = SingleInstanceGuard(tmp_path), SingleInstanceGuard(tmp_path)
    first.acquire()
    with pytest.raises(AlreadyRunningError):
        second.acquire()
    first.close()
    second.acquire()
    second.close()


def test_phase6_diagnostics_and_operator_cli_are_secret_safe(tmp_path, capsys):
    settings = _settings(tmp_path).model_copy(
        update={
            "funpay_session": "PHASE6_FUNPAY_SESSION_SECRET",
            "gmail_refresh_token": "PHASE6_GMAIL_REFRESH_SECRET",
            "pixelstorm_password": "PHASE6_PIXELSTORM_PASSWORD_SECRET",
            "owner_notifier_token": "PHASE6_OWNER_TOKEN_SECRET",
        }
    )
    _migrate(settings)
    database = Database(settings.database_url)
    bundle = create_diagnostics(settings, database, preflight(settings, database), tmp_path / "diagnostics.zip")
    with zipfile.ZipFile(bundle) as archive:
        contents = "".join(archive.read(name).decode("utf-8") for name in archive.namelist())
    assert "PHASE6_" not in contents
    assert manage(["--database-url", settings.database_url, "--runtime-dir", str(settings.runtime_dir), "status"]) == 0
    assert "PHASE6_" not in capsys.readouterr().out


def test_phase6_supervision_backoff_and_notification_failure_isolation():
    task = SupervisedTask("funpay", base_backoff_seconds=1)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert task.run(now, lambda: (_ for _ in ()).throw(RuntimeError("offline"))) == 1
    assert task.run(now, lambda: (_ for _ in ()).throw(RuntimeError("offline"))) == 2
    assert task.health.consecutive_failures == 2
    assert task.run(now, lambda: None) == 0
    assert task.health.consecutive_failures == 0

    class BrokenNotifier:
        def notify(self, notification):
            raise RuntimeError(notification.category)

    notifier = CooldownOwnerNotifier(BrokenNotifier(), 60)
    notifier.notify(OwnerNotification("AUTH_REQUIRED", "a", now))
    sink = FakeOwnerNotifier()
    cooldown = CooldownOwnerNotifier(sink, 60)
    cooldown.notify(OwnerNotification("AUTH_REQUIRED", "a", now))
    cooldown.notify(OwnerNotification("AUTH_REQUIRED", "b", now + timedelta(seconds=1)))
    cooldown.recovered("AUTH_REQUIRED")
    cooldown.notify(OwnerNotification("AUTH_REQUIRED", "c", now + timedelta(seconds=2)))
    assert len(sink.notifications) == 2


def test_phase6_stuck_cleanup_and_consistency_diagnostics(tmp_path):
    settings = _settings(tmp_path)
    _migrate(settings)
    database = Database(settings.database_url)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with database.engine.begin() as connection:
        connection.execute(
            __import__("sqlalchemy").text(
                "INSERT INTO accounts(id,code,status,credential_version,state_version,created_at,updated_at) "
                "VALUES ('a','A','AVAILABLE',1,0,:now,:now)"
            ),
            {"now": now},
        )
        connection.execute(
            __import__("sqlalchemy").text(
                "INSERT INTO operations(id,kind,idempotency_key,status,account_id,correlation_id,attempt_count,security_state,created_at,safe_metadata) "
                "VALUES ('o','SEND_CREDENTIALS','o','PENDING','a','o',0,'INIT',:now,'{}')"
            ),
            {"now": now - timedelta(days=40)},
        )
    assert stuck_operations(database, now, threshold_seconds=60)[0]["operation_id"] == "o"
    assert consistency_checks(database)[0].state == CheckState.READY
    with database.engine.begin() as connection:
        connection.execute(__import__("sqlalchemy").text("UPDATE operations SET status='COMPLETED', completed_at=:now WHERE id='o'"), {"now": now - timedelta(days=40)})
    assert cleanup_metadata(database, now, 30)["operations"] == 1


def test_phase6_corrupt_vault_and_corrupt_database_fail_closed(tmp_path):
    settings = _settings(tmp_path)
    settings.secure_store_path.mkdir(parents=True)
    assert vault_check(settings.secure_store_path, "SECURE_STORE").state == CheckState.NOT_READY
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a database")
    runtime = create_production_application(
        settings.model_copy(update={"database_url": f"sqlite:///{corrupt.as_posix()}"})
    )
    with pytest.raises(Exception, match="DATABASE_INTEGRITY_FAILED"):
        runtime.start()


def test_phase6_system_secret_markers_never_reach_logs_or_safe_outputs(tmp_path):
    markers = {
        "funpay_session": "PHASE6_FUNPAY_SESSION_SECRET",
        "gmail_refresh_token": "PHASE6_GMAIL_REFRESH_SECRET",
        "pixelstorm_password": "PHASE6_PIXELSTORM_PASSWORD_SECRET",
        "owner_notifier_token": "PHASE6_OWNER_TOKEN_SECRET",
        "golden_key": "PHASE6_BROWSER_SESSION_SECRET",
    }
    settings = _settings(tmp_path).model_copy(update=markers)
    logger = configure_safe_logging(settings)
    logger.error("failure %s", markers["funpay_session"], extra={"event_category": markers["gmail_refresh_token"]})
    for handler in logger.handlers:
        handler.flush()
    _migrate(settings)
    database = Database(settings.database_url)
    output = json.dumps(preflight(settings, database), default=str) + settings.log_path.read_text(encoding="utf-8")
    assert all(marker not in output for marker in markers.values())


def test_phase6_production_restart_and_manual_review_are_safe(tmp_path):
    settings = _settings(tmp_path)
    _migrate(settings)
    database = Database(settings.database_url)
    repository = Repository(database)
    account_id = repository.add_account("phase6-manual", datetime(2026, 1, 1, tzinfo=UTC))
    with database.engine.begin() as connection:
        connection.execute(
            __import__("sqlalchemy").text("UPDATE accounts SET status='MANUAL_REVIEW' WHERE id=:id"),
            {"id": account_id},
        )
    repository.add_account_lot(account_id, "phase6-manual-lot", datetime(2026, 1, 1, tzinfo=UTC))
    first = create_production_application(settings)
    try:
        assert first.start()
        assert first.accepting_work is False
    finally:
        first.shutdown()
    second = create_production_application(settings)
    try:
        assert second.start()
        assert second.accepting_work is False
    finally:
        second.shutdown()


def test_phase6_shutdown_api_stops_new_cycles_and_releases_runtime_lock(tmp_path):
    settings = _settings(tmp_path)
    _migrate(settings)
    runtime = create_production_application(settings)
    _seed_rentable(runtime)
    assert runtime.start()
    runtime.request_shutdown()
    assert runtime.closed and runtime.accepting_work is False
    with pytest.raises(Exception, match="RUNTIME_STOPPED"):
        runtime.run_once(datetime(2026, 1, 1, tzinfo=UTC))
    successor = create_production_application(settings)
    try:
        assert successor.start()
    finally:
        successor.shutdown()


def test_phase6_browser_sessions_are_memory_only_and_closed(tmp_path):
    class Page:
        url = "https://login.pixstorm.ru/login"

        def set_default_timeout(self, value):
            self.timeout = value

        def goto(self, url):
            self.url = url

    class Context:
        def __init__(self):
            self.closed = False

        def new_page(self):
            return Page()

        def close(self):
            self.closed = True

    class Browser:
        def __init__(self):
            self.contexts = []

        def new_context(self, **kwargs):
            del kwargs
            context = Context()
            self.contexts.append(context)
            return context

    browser = Browser()
    factory = PixelStormBrowserSessionFactory(browser, FakeSecureStore(), "https://login.pixstorm.ru/login")
    factory.open_account_page("account")
    factory.open_verification_page("account")
    factory.close()
    assert all(context.closed for context in browser.contexts)
    assert not list(tmp_path.rglob("storage_state.json"))


def test_phase6_manage_migrate_db_check_backup_and_backup_check(tmp_path, capsys):
    settings = _settings(tmp_path)
    args = ["--database-url", settings.database_url, "--runtime-dir", str(settings.runtime_dir)]
    assert manage([*args, "migrate"]) == 0
    assert manage([*args, "db-check"]) == 0
    assert manage([*args, "backup"]) == 0
    backup = Path(capsys.readouterr().out.strip().splitlines()[-1])
    assert manage([*args, "backup-check", str(backup)]) == 0


def test_phase6_main_production_dry_run_demo(tmp_path, capsys):
    settings = _settings(tmp_path)
    _migrate(settings)
    database = Database(settings.database_url)
    account_id = Repository(database).add_account("phase6-cli", datetime(2026, 1, 1, tzinfo=UTC))
    Repository(database).add_account_lot(account_id, "phase6-cli-lot", datetime(2026, 1, 1, tzinfo=UTC))
    from app.adapters.production_stores import DPAPISecretProtector, ProductionSecureStore

    ProductionSecureStore(settings.secure_store_path, DPAPISecretProtector()).set_current_credentials(account_id, "login", "password")
    run_application(
        ["--dry-run", "--database-url", settings.database_url, "--runtime-dir", str(settings.runtime_dir)]
    )
    assert "READY" in capsys.readouterr().out
