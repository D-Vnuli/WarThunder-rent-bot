from datetime import timedelta
from multiprocessing import get_context
from pathlib import Path

import pytest
from sqlalchemy import select

from app.adapters.fake import (
    FakeEphemeralEmailSecretStore,
    FakeFunPayAdapter,
    FakeOwnerNotifier,
    FakePixelStormAdapter,
    FakeSecureStore,
    PersistentFakePixelStormBackend,
    PersistentFakeSecureStore,
)
from app.adapters.pixelstorm import (
    PixelStormBrowserContextFactory,
    PixelStormBrowserSafety,
    PixelStormBrowserSessionFactory,
    PlaywrightPixelStormAdapter,
)
from app.application.password_rotator import PasswordRotator
from app.application.pixelstorm_otp import PixelStormMaintenanceOtpService
from app.application.pixelstorm_security import PixelStormSecurityService
from app.application.rental_manager import RentalManager
from app.application.startup_reconciliation import StartupReconciliation
from app.domain.models import ClassifiedEmailEvent, OrderInput
from app.domain.notifications import OwnerNotification
from app.domain.pixelstorm import (
    PixelStormAuthResult,
    PixelStormCredentialResult,
    PixelStormHealth,
    PixelStormPasswordChangeResult,
    PixelStormRevocationResult,
    PixelStormSecurityCapabilities,
)
from app.domain.states import (
    AccountStatus,
    EmailMessageType,
    EmailPayloadState,
    OperationKind,
    OperationStatus,
    RentalStatus,
)
from app.persistence.classified_email_events import ClassifiedEmailRepository
from app.persistence.database import Database
from app.persistence.models import AuditEventRow, ClassifiedEmailEventRow, OperationRow
from app.persistence.repositories import Repository
from tests.helpers import create_test_account


def _revoke_worker(path: str, remote_path: str, account_id: str, now) -> bool:
    repo = Repository(Database(f"sqlite:///{Path(path).as_posix()}"))
    operation = next((item for item in repo.pending_operations() if item.kind == OperationKind.REVOKE_SESSIONS), None)
    if operation is None or repo.claim_operation(operation.id, now) is None:
        return False
    repo.prepare_operation(operation.id, now)
    service = PixelStormSecurityService(FakePixelStormAdapter(PersistentFakePixelStormBackend(remote_path)), FakeSecureStore(), repository=repo)
    if not service.revoke(account_id, operation.id, now):
        return False
    repo.operation_completed(operation.id, now)
    return True


def _rotate_worker(path: str, remote_path: str, vault_path: str, account_id: str, now) -> bool:
    repo = Repository(Database(f"sqlite:///{Path(path).as_posix()}"))
    operation = next((item for item in repo.pending_operations() if item.kind == OperationKind.ROTATE_PASSWORD), None)
    if operation is None or repo.claim_operation(operation.id, now) is None:
        return False
    secrets = PersistentFakeSecureStore(vault_path)
    service = PixelStormSecurityService(FakePixelStormAdapter(PersistentFakePixelStormBackend(remote_path)), secrets, repository=repo)
    if not service.rotate(account_id, operation.id, now):
        return False
    repo.operation_completed(operation.id, now)
    return True


def _startup_security_worker(path: str, remote_path: str, vault_path: str, now) -> bool:
    repo = Repository(Database(f"sqlite:///{Path(path).as_posix()}"))
    secrets = PersistentFakeSecureStore(vault_path)
    pixel = FakePixelStormAdapter(PersistentFakePixelStormBackend(remote_path))
    manager = RentalManager(
        repo,
        FakeFunPayAdapter(),
        None,
        secrets,
        pixelstorm_security=PixelStormSecurityService(pixel, secrets, repository=repo),
    )
    return StartupReconciliation(repo, manager).run(now) > 0


def _startup_claim_only_worker(path: str, operation_id: str, now) -> str | None:
    repo = Repository(Database(f"sqlite:///{Path(path).as_posix()}"))
    claimed = repo.claim_startup_recovery(operation_id, now)
    return claimed.recovery_claim_token if claimed is not None else None


def _pixel_manager(repository, manager, account_id, *, notifier=None):
    secrets = repository._test_secret_store  # type: ignore[attr-defined]
    pixel = FakePixelStormAdapter()
    current = secrets.get_current_credentials(account_id)
    assert current is not None
    pixel.set_credentials(account_id, *current)
    manager._pixelstorm_security = PixelStormSecurityService(pixel, secrets, notifier)  # type: ignore[attr-defined]
    return pixel, secrets


def _operation(repository, kind):
    with repository.db.session() as session:
        return session.scalar(select(OperationRow).where(OperationRow.kind == kind).order_by(OperationRow.created_at.desc()))


class _SyntheticVerificationFactory:
    """Fresh synthetic login context per verification; never returns an account page."""

    def __init__(self, browser, html: str) -> None:
        self._browser = browser
        self._html = html
        self.contexts = []

    def open_verification_page(self, account_id: str):
        del account_id
        context = self._browser.new_context()
        self.contexts.append(context)
        page = context.new_page()
        page.route("**/*", lambda route: route.fulfill(status=200, body=self._html, content_type="text/html"))
        page.goto("https://login.pixstorm.ru/login")
        return page


def _file_waiting_operation(tmp_path, now, *, password_change: bool):
    path = tmp_path / ("waiting-password.db" if password_change else "waiting-otp.db")
    database = Database(f"sqlite:///{path.as_posix()}")
    database.create_schema()
    repository = Repository(database)
    funpay = FakeFunPayAdapter()
    account_id = repository.add_account("PS-RESTART", now)
    repository.add_account_lot(account_id, "lot-restart", now)
    funpay.set_lot_state("lot-restart", enabled=True)
    secrets = FakeSecureStore()
    secrets.set_current_credentials(account_id, "restart-login", "restart-current")
    pixel = FakePixelStormAdapter()
    pixel.set_credentials(account_id, "restart-login", "restart-current")
    events = ClassifiedEmailRepository(repository.db)
    ephemeral = FakeEphemeralEmailSecretStore()
    service = PixelStormSecurityService(
        pixel,
        secrets,
        repository=repository,
        maintenance_otp=PixelStormMaintenanceOtpService(events, ephemeral),
        password_rotator=PasswordRotator(events, ephemeral),
    )
    manager = RentalManager(repository, funpay, None, secrets, pixelstorm_security=service)
    started = manager.accept_order(OrderInput("restart-order", "buyer", "6H", 1), now)
    assert started.rental_id is not None
    manager.run_operations(now)
    manager.run_operations(now)
    repository.expire_due(now + timedelta(seconds=2))
    if password_change:
        pixel.require_password_email_confirmation(account_id)
        manager.run_operations(now + timedelta(seconds=2))
        manager.run_operations(now + timedelta(seconds=2))
        operation = _operation(repository, OperationKind.ROTATE_PASSWORD)
    else:
        pixel.set_health(account_id, PixelStormHealth.AUTH_REQUIRED)
        pixel.set_auth_results(account_id, [PixelStormAuthResult.EMAIL_OTP_REQUIRED])
        manager.run_operations(now + timedelta(seconds=2))
        operation = _operation(repository, OperationKind.REVOKE_SESSIONS)
    assert operation is not None and operation.status == OperationStatus.RUNNING
    return path, repository, funpay, account_id, secrets, events, ephemeral, operation, started.rental_id


class _DeterministicPasswordGenerator:
    def generate(self) -> str:
        return "UNIQUE_PENDING_PASSWORD_SECRET_63C9"


@pytest.mark.parametrize(
    ("fixture", "origin", "expected_health", "expected_auth"),
    [
        ("login.html", "https://login.pixstorm.ru/login", PixelStormHealth.AUTH_REQUIRED, "BAD_CREDENTIALS"),
        ("otp.html", "https://login.pixstorm.ru/login", PixelStormHealth.AUTH_REQUIRED, "EMAIL_OTP_REQUIRED"),
        ("pixel-pass.html", "https://login.pixstorm.ru/login", PixelStormHealth.PIXEL_PASS_REQUIRED, "PIXEL_PASS_REQUIRED"),
        ("challenge.html", "https://login.pixstorm.ru/login", PixelStormHealth.CHALLENGE, "CHALLENGE"),
        ("expired.html", "https://login.pixstorm.ru/login", PixelStormHealth.AUTH_REQUIRED, "BAD_CREDENTIALS"),
        ("authenticated.html", "https://login.pixstorm.ru/account", PixelStormHealth.READY, "SUCCESS"),
        ("unknown.html", "https://login.pixstorm.ru/account", PixelStormHealth.UNKNOWN_UI, "UNKNOWN_UI"),
        ("wrong-region-gaijin.html", "https://login.gaijin.net/login", PixelStormHealth.WRONG_REGION, "WRONG_REGION"),
    ],
)
def test_playwright_fixture_boundary_and_secure_session_store(
    core, fixture, origin, expected_health, expected_auth
):
    repository, _manager, _funpay, _gaijin = core
    sessions = FakeSecureStore()
    from playwright.sync_api import sync_playwright

    html = (Path(__file__).parents[1] / "fixtures" / "pixelstorm" / fixture).read_text(encoding="utf8")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context()
        page = context.new_page()
        page.route("**/*", lambda route: route.fulfill(status=200, body=html, content_type="text/html"))
        page.goto(origin)
        adapter = PlaywrightPixelStormAdapter(sessions, {"a": page})
        assert adapter.health("a") == expected_health
        assert adapter._inspection("a").authentication.value == expected_auth
        assert adapter.inspect_security_capabilities("a").unknown
        browser.close()
    assert sessions.get_pixelstorm_session("a") is None
    assert repository.account_lot_ids("missing") == []
    assert PixelStormBrowserSafety().trace == "off"
    assert PixelStormBrowserSafety().video == "off"
    assert not PixelStormBrowserSafety().screenshots


def test_playwright_login_contract_rejects_wrong_password(core):
    from playwright.sync_api import sync_playwright

    html = (Path(__file__).parents[1] / "fixtures" / "pixelstorm" / "login.html").read_text(encoding="utf8")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.route("**/*", lambda route: route.fulfill(status=200, body=html, content_type="text/html"))
        page.goto("https://login.pixstorm.ru/login")
        adapter = PlaywrightPixelStormAdapter(FakeSecureStore(), {"a": page}).with_credential_verification_factory(
            _SyntheticVerificationFactory(browser, html)
        )
        assert adapter.verify_credentials("a", "wrong", "wrong") == PixelStormCredentialResult.INVALID
        assert adapter.verify_credentials("a", "synthetic-login", "synthetic-password") == PixelStormCredentialResult.VALID
        browser.close()


def test_playwright_authenticated_page_cannot_validate_wrong_supplied_credentials(core):
    from playwright.sync_api import sync_playwright

    fixtures = Path(__file__).parents[1] / "fixtures" / "pixelstorm"
    authenticated = (fixtures / "authenticated.html").read_text(encoding="utf8")
    login = (fixtures / "login.html").read_text(encoding="utf8")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.route("**/*", lambda route: route.fulfill(status=200, body=authenticated, content_type="text/html"))
        page.goto("https://login.pixstorm.ru/account")
        factory = _SyntheticVerificationFactory(browser, login)
        adapter = PlaywrightPixelStormAdapter(FakeSecureStore(), {"a": page}).with_credential_verification_factory(factory)
        assert adapter.health("a") == PixelStormHealth.READY
        assert adapter.verify_credentials("a", "WRONG_LOGIN", "WRONG_PASSWORD") == PixelStormCredentialResult.INVALID
        assert adapter.verify_credentials("a", "synthetic-login", "synthetic-password") == PixelStormCredentialResult.VALID
        assert len(factory.contexts) == 2
        browser.close()


def test_playwright_rotation_cannot_promote_from_an_authenticated_page_without_fresh_verification(
    core, now
):
    from playwright.sync_api import sync_playwright

    html = (Path(__file__).parents[1] / "fixtures" / "pixelstorm" / "password-change.html").read_text(encoding="utf8")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.route("**/*", lambda route: route.fulfill(status=200, body=html, content_type="text/html"))
        page.goto("https://login.pixstorm.ru/account")
        secrets = FakeSecureStore()
        secrets.set_current_credentials("a", "synthetic-login", "synthetic-current")
        service = PixelStormSecurityService(PlaywrightPixelStormAdapter(secrets, {"a": page}), secrets)
        assert not service.rotate("a", "rotation", now)
        assert secrets.get_current_credentials("a") == ("synthetic-login", "synthetic-current")
        assert secrets.get_pending_credentials("a") is not None
        assert page.locator("[data-pixelstorm-page]").get_attribute("data-pixelstorm-password-state") == "ready"
        browser.close()


def test_playwright_synthetic_otp_password_revoke_and_safe_context(core, tmp_path):
    from playwright.sync_api import sync_playwright

    fixture_dir = Path(__file__).parents[1] / "fixtures" / "pixelstorm"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = PixelStormBrowserContextFactory().new_context(browser)
        page = context.new_page()

        def load(name: str) -> PlaywrightPixelStormAdapter:
            page.route("**/*", lambda route: route.fulfill(status=200, body=(fixture_dir / name).read_text(encoding="utf8"), content_type="text/html"))
            page.goto("https://login.pixstorm.ru/synthetic")
            page.unroute("**/*")
            return PlaywrightPixelStormAdapter(FakeSecureStore(), {"a": page})

        otp = load("otp.html")
        assert otp.authenticate("a", "ignored", "ignored", otp="wrong") == PixelStormAuthResult.EMAIL_OTP_REQUIRED
        otp = load("otp.html")
        assert otp.authenticate("a", "ignored", "ignored", otp="synthetic-otp") == PixelStormAuthResult.SUCCESS

        password = load("password-change.html")
        assert password.request_password_change("a", "ignored", "wrong") == PixelStormPasswordChangeResult.INVALID
        assert password.request_password_change("a", "ignored", "synthetic-current") == PixelStormPasswordChangeResult.CONFIRMATION_REQUIRED
        assert password.complete_password_change("a", "memory-only-reset-url", "synthetic-new") == PixelStormPasswordChangeResult.VERIFIED

        revoke = load("security-history.html")
        assert revoke.inspect_security_capabilities("a").session_revocation_available
        assert revoke.revoke_sessions("a") == PixelStormRevocationResult.SUPPORTED_VERIFIED
        assert revoke.verify_revocation("a") == PixelStormRevocationResult.SUPPORTED_VERIFIED
        unsupported = load("security-unsupported.html")
        assert unsupported.revoke_sessions("a") == PixelStormRevocationResult.UNSUPPORTED
        with pytest.raises(ValueError):
            PixelStormBrowserContextFactory().new_context(browser, debug_artifact_dir=tmp_path / "artifacts")
        context.close()
        browser.close()


def test_playwright_pixel_pass_otp_cannot_be_bypassed(core):
    from playwright.sync_api import sync_playwright

    html = (Path(__file__).parents[1] / "fixtures" / "pixelstorm" / "pixel-pass.html").read_text(encoding="utf8")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.route("**/*", lambda route: route.fulfill(status=200, body=html, content_type="text/html"))
        page.goto("https://login.pixstorm.ru/login")
        assert PlaywrightPixelStormAdapter(FakeSecureStore(), {"a": page}).authenticate("a", "x", "y", otp="synthetic-otp") == PixelStormAuthResult.PIXEL_PASS_REQUIRED
        browser.close()


def test_playwright_browser_session_factory_restores_saves_and_clears_only_in_memory(core, tmp_path):
    from playwright.sync_api import sync_playwright

    marker = "UNIQUE_PIXELSTORM_SESSION_SECRET"
    fixtures = Path(__file__).parents[1] / "fixtures" / "pixelstorm"
    login_html = (fixtures / "login.html").read_text(encoding="utf8")
    session_state = '{"cookies":[{"name":"session","value":"' + marker + '","domain":"login.pixstorm.ru","path":"/"}],"origins":[]}'
    sessions = FakeSecureStore()
    sessions.set_pixelstorm_session("a", session_state)

    def initialize(context, url):
        context.route("**/*", lambda route: route.fulfill(status=200, body=login_html, content_type="text/html"))
        page = context.new_page()
        page.goto(url)
        return page

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        factory = PixelStormBrowserSessionFactory(browser, sessions, "https://login.pixstorm.ru/login", page_initializer=initialize)
        restored = factory.open_account_page("a")
        assert restored.context.cookies()[0]["value"] == marker
        adapter = PlaywrightPixelStormAdapter(sessions, {"a": restored}).with_browser_sessions(factory)
        assert adapter.health("a") == PixelStormHealth.AUTH_REQUIRED
        assert adapter.authenticate("a", "synthetic-login", "synthetic-password") == PixelStormAuthResult.SUCCESS
        saved = sessions.get_pixelstorm_session("a")
        assert saved is not None and marker in saved
        restarted = PixelStormBrowserSessionFactory(browser, sessions, "https://login.pixstorm.ru/login", page_initializer=initialize)
        assert restarted.open_account_page("a").context.cookies()[0]["value"] == marker
        sessions.set_pixelstorm_session("invalid", "not-json")
        invalid = restarted.open_account_page("invalid")
        assert sessions.get_pixelstorm_session("invalid") is None
        assert PlaywrightPixelStormAdapter(sessions, {"invalid": invalid}).health("invalid") == PixelStormHealth.AUTH_REQUIRED
        browser.close()

    database = Database(f"sqlite:///{(tmp_path / 'session-scan.db').as_posix()}")
    database.create_schema()
    database.engine.dispose()
    assert marker not in (tmp_path / "session-scan.db").read_bytes().decode("latin1")
    assert not [path for path in tmp_path.rglob("*") if path.is_file() and marker in path.read_bytes().decode("latin1", errors="ignore")]


def test_pixel_pass_wrong_region_and_unknown_fail_closed(core, now):
    repository, manager, funpay, _gaijin = core
    account_id = create_test_account(repository, funpay, "PS01", now)
    notifier = FakeOwnerNotifier()
    pixel, _secrets = _pixel_manager(repository, manager, account_id, notifier=notifier)
    for health, category in [(PixelStormHealth.PIXEL_PASS_REQUIRED, "PIXEL_PASS_REQUIRED"), (PixelStormHealth.WRONG_REGION, "PIXEL_STORM_WRONG_REGION"), (PixelStormHealth.UNKNOWN_UI, "PIXEL_STORM_UNKNOWN_UI")]:
        pixel.set_health(account_id, health)
        assert not manager._pixelstorm_security.revoke(account_id, health, now)  # type: ignore[attr-defined]
        assert notifier.notifications[-1].category == category


def test_bounded_maintenance_auth_and_correlated_otp(core, now):
    repository, manager, funpay, _gaijin = core
    account_id = create_test_account(repository, funpay, "PS-AUTH", now)
    pixel, secrets = _pixel_manager(repository, manager, account_id)
    result = manager.accept_order(OrderInput("ps-auth", "buyer", "6H", 1), now)
    manager.run_operations(now)
    manager.run_operations(now)
    repository.expire_due(now + timedelta(seconds=2))
    operation = _operation(repository, OperationKind.REVOKE_SESSIONS)
    assert operation is not None
    assert repository.claim_operation(operation.id, now + timedelta(seconds=2))
    assert repository.prepare_operation(operation.id, now + timedelta(seconds=2))
    pixel.set_health(account_id, PixelStormHealth.AUTH_REQUIRED)
    pixel.set_auth_results(account_id, [PixelStormAuthResult.SUCCESS])
    assert manager._pixelstorm_security.revoke(account_id, operation.id, now + timedelta(seconds=2))  # type: ignore[attr-defined]
    assert pixel.authentication_calls == [account_id]

    # A fresh maintenance OTP is consumed once and only after login was requested.
    ephemeral = FakeEphemeralEmailSecretStore()
    events = ClassifiedEmailRepository(repository.db)
    event = ClassifiedEmailEvent(
        "maintenance-otp",
        "maintenance-otp-mail",
        EmailMessageType.LOGIN_OTP,
        now + timedelta(seconds=4),
        account_id,
        None,
        EmailPayloadState.AVAILABLE,
    )
    assert events.store_event(event, now)
    assert ephemeral.put(event.id, "UNIQUE_PIXELSTORM_OTP_SECRET", expires_at=now + timedelta(seconds=60))
    manager._pixelstorm_security = PixelStormSecurityService(  # type: ignore[attr-defined]
        pixel,
        secrets,
        repository=repository,
        maintenance_otp=PixelStormMaintenanceOtpService(events, ephemeral),
    )
    pixel.set_health(account_id, PixelStormHealth.AUTH_REQUIRED)
    pixel.set_auth_results(account_id, [PixelStormAuthResult.EMAIL_OTP_REQUIRED, PixelStormAuthResult.SUCCESS])
    assert manager._pixelstorm_security.revoke(account_id, operation.id, now + timedelta(seconds=4))  # type: ignore[attr-defined]
    assert pixel.authentication_calls[-2:] == [account_id, account_id]
    assert events.get_event("maintenance-otp-mail").payload_state == EmailPayloadState.CONSUMED
    assert result.rental_id is not None


def test_pixel_pass_does_not_bypass_maintenance_otp(core, now):
    repository, manager, funpay, _gaijin = core
    account_id = create_test_account(repository, funpay, "PS-AUTH-PASS", now)
    pixel, _secrets = _pixel_manager(repository, manager, account_id)
    pixel.set_health(account_id, PixelStormHealth.AUTH_REQUIRED)
    pixel.set_auth_results(account_id, [PixelStormAuthResult.PIXEL_PASS_REQUIRED])
    assert not manager._pixelstorm_security.revoke(account_id, "no-op", now)  # type: ignore[attr-defined]
    assert len(pixel.authentication_calls) == 1


def test_delayed_maintenance_otp_keeps_operation_waiting_then_resumes(core, now):
    repository, manager, funpay, _gaijin = core
    account_id = create_test_account(repository, funpay, "PS-WAIT", now)
    pixel, secrets = _pixel_manager(repository, manager, account_id)
    events = ClassifiedEmailRepository(repository.db)
    ephemeral = FakeEphemeralEmailSecretStore()
    manager._pixelstorm_security = PixelStormSecurityService(  # type: ignore[attr-defined]
        pixel, secrets, repository=repository, maintenance_otp=PixelStormMaintenanceOtpService(events, ephemeral)
    )
    started = manager.accept_order(OrderInput("wait", "buyer", "6H", 1), now)
    manager.run_operations(now)
    manager.run_operations(now)
    repository.expire_due(now + timedelta(seconds=2))
    pixel.set_health(account_id, PixelStormHealth.AUTH_REQUIRED)
    pixel.set_auth_results(account_id, [PixelStormAuthResult.EMAIL_OTP_REQUIRED, PixelStormAuthResult.SUCCESS])
    manager.run_operations(now + timedelta(seconds=2))
    operation = _operation(repository, OperationKind.REVOKE_SESSIONS)
    assert operation is not None and operation.status == OperationStatus.RUNNING
    assert operation.security_state == "WAITING_LOGIN_OTP"
    assert repository.get_account(account_id).status != AccountStatus.MANUAL_REVIEW
    assert pixel.authentication_calls == [account_id]
    event = ClassifiedEmailEvent("wait-otp", "wait-otp-mail", EmailMessageType.LOGIN_OTP, now + timedelta(seconds=7), account_id, None, EmailPayloadState.AVAILABLE)
    assert events.store_event(event, now + timedelta(seconds=7))
    assert ephemeral.put(event.id, "UNIQUE_PIXELSTORM_OTP_SECRET", expires_at=now + timedelta(minutes=1))
    manager.run_operations(now + timedelta(seconds=7))
    assert pixel.authentication_calls == [account_id, account_id]
    assert _operation(repository, OperationKind.ROTATE_PASSWORD).status == OperationStatus.PENDING
    assert started.rental_id is not None


def test_maintenance_otp_window_rejects_stale_and_future(core, now):
    repository, manager, funpay, _gaijin = core
    account_id = create_test_account(repository, funpay, "PS-OTP-WINDOW", now)
    manager.accept_order(OrderInput("otp-window", "buyer", "6H", 1), now)
    manager.run_operations(now)
    manager.run_operations(now)
    repository.expire_due(now + timedelta(seconds=2))
    operation = _operation(repository, OperationKind.REVOKE_SESSIONS)
    assert operation is not None and repository.claim_operation(operation.id, now + timedelta(seconds=2))
    events = ClassifiedEmailRepository(repository.db, maintenance_otp_correlation_seconds=30)
    for ident, received in [("old", now - timedelta(seconds=1)), ("late", now + timedelta(seconds=31)), ("future", now + timedelta(seconds=6))]:
        assert events.store_event(ClassifiedEmailEvent(ident, f"{ident}-mail", EmailMessageType.LOGIN_OTP, received, account_id, None, EmailPayloadState.AVAILABLE), now)
    assert events.claim_maintenance_login_otp(account_id, operation.id, now, now + timedelta(seconds=5)) is None
    fresh = ClassifiedEmailEvent("fresh-window", "fresh-window-mail", EmailMessageType.LOGIN_OTP, now + timedelta(seconds=5), account_id, None, EmailPayloadState.AVAILABLE)
    assert events.store_event(fresh, now)
    assert events.claim_maintenance_login_otp(account_id, operation.id, now, now + timedelta(seconds=5)) is not None


def test_delayed_password_change_email_keeps_rotation_waiting_then_resumes(core, now):
    repository, manager, funpay, _gaijin = core
    account_id = create_test_account(repository, funpay, "PS-PASSWORD-WAIT", now)
    pixel, secrets = _pixel_manager(repository, manager, account_id)
    events = ClassifiedEmailRepository(repository.db)
    ephemeral = FakeEphemeralEmailSecretStore()
    manager._pixelstorm_security = PixelStormSecurityService(  # type: ignore[attr-defined]
        pixel,
        secrets,
        repository=repository,
        password_rotator=PasswordRotator(events, ephemeral),
    )
    pixel.require_password_email_confirmation(account_id)
    manager.accept_order(OrderInput("wait-password", "buyer", "6H", 1), now)
    manager.run_operations(now)
    manager.run_operations(now)
    repository.expire_due(now + timedelta(seconds=2))
    manager.run_operations(now + timedelta(seconds=2))
    manager.run_operations(now + timedelta(seconds=2))
    rotation = _operation(repository, OperationKind.ROTATE_PASSWORD)
    assert rotation is not None and rotation.status == OperationStatus.RUNNING
    assert rotation.security_state == "WAITING_PASSWORD_CHANGE_EMAIL"
    assert rotation.password_change_requested_at == now + timedelta(seconds=2)
    assert pixel.password_change_requests == [account_id]
    for second in (3, 4, 5):
        manager.run_operations(now + timedelta(seconds=second))
    waiting = _operation(repository, OperationKind.ROTATE_PASSWORD)
    assert waiting is not None and waiting.status == OperationStatus.RUNNING
    assert waiting.security_state == "WAITING_PASSWORD_CHANGE_EMAIL"
    assert pixel.password_change_requests == [account_id]
    assert events.expected_rotation_operation(account_id, now + timedelta(seconds=7)) == rotation.id
    reset = "https://login.pixstorm.ru/ru/sso/changePassword/000000?token=UNIQUE_RESET_TOKEN_SECRET"
    event = ClassifiedEmailEvent(
        "wait-password-event", "wait-password-mail", EmailMessageType.PASSWORD_CHANGE,
        now + timedelta(seconds=7), account_id, rotation.id, EmailPayloadState.AVAILABLE,
    )
    assert events.store_event(event, now + timedelta(seconds=7))
    assert ephemeral.put(event.id, reset, expires_at=now + timedelta(minutes=1))
    manager.run_operations(now + timedelta(seconds=7))
    assert repository.get_account(account_id).status == AccountStatus.AVAILABLE_OFFLINE
    assert pixel.password_change_requests == [account_id]


def test_startup_recovery_creates_waiting_login_otp_and_normal_worker_resumes(core, now):
    repository, manager, funpay, _gaijin = core
    account_id = create_test_account(repository, funpay, "PS-STARTUP-OTP", now)
    pixel, secrets = _pixel_manager(repository, manager, account_id)
    events = ClassifiedEmailRepository(repository.db)
    ephemeral = FakeEphemeralEmailSecretStore()
    manager._pixelstorm_security = PixelStormSecurityService(  # type: ignore[attr-defined]
        pixel,
        secrets,
        repository=repository,
        maintenance_otp=PixelStormMaintenanceOtpService(events, ephemeral),
    )
    started = manager.accept_order(OrderInput("startup-otp", "buyer", "6H", 1), now)
    assert started.rental_id is not None
    manager.run_operations(now)
    manager.run_operations(now)
    repository.expire_due(now + timedelta(seconds=2))
    revoke = _operation(repository, OperationKind.REVOKE_SESSIONS)
    assert revoke is not None and repository.claim_operation(revoke.id, now + timedelta(seconds=2))
    pixel.set_health(account_id, PixelStormHealth.AUTH_REQUIRED)
    pixel.set_auth_results(account_id, [PixelStormAuthResult.EMAIL_OTP_REQUIRED])
    StartupReconciliation(repository, manager, funpay).run(now + timedelta(seconds=33))
    waiting = repository.get_operation(revoke.id)
    assert waiting.status == OperationStatus.RUNNING
    assert waiting.security_state == "WAITING_LOGIN_OTP"
    assert waiting.recovery_claim_token is None
    assert repository.get_account(account_id).status == AccountStatus.REVOKING
    assert repository.get_rental(started.rental_id).status == RentalStatus.REVOKING
    event = ClassifiedEmailEvent("startup-otp", "startup-otp-mail", EmailMessageType.LOGIN_OTP, now + timedelta(seconds=34), account_id, None, EmailPayloadState.AVAILABLE)
    assert events.store_event(event, now + timedelta(seconds=34))
    assert ephemeral.put(event.id, "OTP", expires_at=now + timedelta(minutes=2))
    pixel.set_auth_results(account_id, [PixelStormAuthResult.SUCCESS])
    manager.run_operations(now + timedelta(seconds=34))
    assert repository.get_operation(revoke.id).status == OperationStatus.COMPLETED


def test_startup_recovery_creates_waiting_password_email_and_normal_worker_resumes(core, now):
    repository, manager, funpay, _gaijin = core
    account_id = create_test_account(repository, funpay, "PS-STARTUP-PASSWORD", now)
    pixel, secrets = _pixel_manager(repository, manager, account_id)
    events = ClassifiedEmailRepository(repository.db)
    ephemeral = FakeEphemeralEmailSecretStore()
    manager._pixelstorm_security = PixelStormSecurityService(  # type: ignore[attr-defined]
        pixel,
        secrets,
        repository=repository,
        password_rotator=PasswordRotator(events, ephemeral),
    )
    started = manager.accept_order(OrderInput("startup-password", "buyer", "6H", 1), now)
    assert started.rental_id is not None
    manager.run_operations(now)
    manager.run_operations(now)
    repository.expire_due(now + timedelta(seconds=2))
    revoke = _operation(repository, OperationKind.REVOKE_SESSIONS)
    assert revoke is not None and repository.claim_operation(revoke.id, now + timedelta(seconds=2))
    assert repository.prepare_operation(revoke.id, now + timedelta(seconds=2))
    assert manager._pixelstorm_security.revoke(account_id, revoke.id, now + timedelta(seconds=2))  # type: ignore[attr-defined]
    assert repository.operation_completed(revoke.id, now + timedelta(seconds=2))
    rotation = _operation(repository, OperationKind.ROTATE_PASSWORD)
    assert rotation is not None and repository.claim_operation(rotation.id, now + timedelta(seconds=2))
    pixel.require_password_email_confirmation(account_id)
    StartupReconciliation(repository, manager, funpay).run(now + timedelta(seconds=33))
    waiting = repository.get_operation(rotation.id)
    assert waiting.status == OperationStatus.RUNNING
    assert waiting.security_state == "WAITING_PASSWORD_CHANGE_EMAIL"
    assert waiting.recovery_claim_token is None
    assert repository.get_account(account_id).status == AccountStatus.ROTATING_PASSWORD
    event = ClassifiedEmailEvent("startup-password", "startup-password-mail", EmailMessageType.PASSWORD_CHANGE, now + timedelta(seconds=34), account_id, rotation.id, EmailPayloadState.AVAILABLE)
    assert events.store_event(event, now + timedelta(seconds=34))
    assert ephemeral.put(event.id, "https://login.pixstorm.ru/ru/sso/changePassword/000000?token=SAFE", expires_at=now + timedelta(minutes=2))
    manager.run_operations(now + timedelta(seconds=34))
    assert repository.get_operation(rotation.id).status == OperationStatus.COMPLETED


def test_password_change_correlation_requires_durable_request_timestamp(core, now):
    repository, manager, funpay, _gaijin = core
    account_id = create_test_account(repository, funpay, "PS-CORRELATION", now)
    _pixel, _secrets = _pixel_manager(repository, manager, account_id)
    manager.accept_order(OrderInput("correlation", "buyer", "6H", 1), now)
    manager.run_operations(now)
    manager.run_operations(now)
    repository.expire_due(now + timedelta(seconds=2))
    manager.run_operations(now + timedelta(seconds=2))
    rotation = _operation(repository, OperationKind.ROTATE_PASSWORD)
    assert rotation is not None and repository.claim_operation(rotation.id, now + timedelta(seconds=2))
    events = ClassifiedEmailRepository(repository.db)
    # RUNNING and started is insufficient before the request intent exists.
    assert events.expected_rotation_operation(account_id, now + timedelta(seconds=3)) is None
    assert repository.record_password_change_requested(rotation.id, now + timedelta(seconds=4))
    assert events.expected_rotation_operation(account_id, now + timedelta(seconds=3)) is None
    assert events.expected_rotation_operation(account_id, now + timedelta(seconds=9)) == rotation.id
    assert events.expected_rotation_operation(account_id, now + timedelta(hours=24)) is None
    with repository.db.session() as session, session.begin():
        session.add(
            OperationRow(
                kind=OperationKind.ROTATE_PASSWORD,
                idempotency_key="ROTATE_PASSWORD:correlation-duplicate",
                status=OperationStatus.RUNNING,
                account_id=account_id,
                rental_id=rotation.rental_id,
                order_id=None,
                correlation_id="correlation-duplicate",
                started_at=now + timedelta(seconds=4),
                password_change_requested_at=now + timedelta(seconds=4),
                created_at=now + timedelta(seconds=4),
            )
        )
    assert events.expected_rotation_operation(account_id, now + timedelta(seconds=9)) is None


def test_password_wait_deadline_preserves_then_fails_closed_without_duplicate_request(core, now):
    repository, manager, funpay, _gaijin = core
    account_id = create_test_account(repository, funpay, "PS-WAIT-DEADLINE", now)
    pixel, secrets = _pixel_manager(repository, manager, account_id)
    events = ClassifiedEmailRepository(repository.db)
    ephemeral = FakeEphemeralEmailSecretStore()
    manager._pixelstorm_security = PixelStormSecurityService(  # type: ignore[attr-defined]
        pixel,
        secrets,
        repository=repository,
        password_rotator=PasswordRotator(events, ephemeral),
    )
    started = manager.accept_order(OrderInput("wait-deadline", "buyer", "6H", 1), now)
    assert started.rental_id is not None
    manager.run_operations(now)
    manager.run_operations(now)
    repository.expire_due(now + timedelta(seconds=2))
    pixel.require_password_email_confirmation(account_id)
    manager.run_operations(now + timedelta(seconds=2))
    manager.run_operations(now + timedelta(seconds=2))
    rotation = _operation(repository, OperationKind.ROTATE_PASSWORD)
    assert rotation is not None and rotation.security_state == "WAITING_PASSWORD_CHANGE_EMAIL"
    assert pixel.password_change_requests == [account_id]
    StartupReconciliation(repository, manager, funpay).run(now + timedelta(minutes=6))
    assert repository.get_operation(rotation.id).status == OperationStatus.RUNNING
    assert repository.get_account(account_id).status == AccountStatus.ROTATING_PASSWORD
    assert repository.get_rental(started.rental_id).status == RentalStatus.PASSWORD_ROTATION
    assert pixel.password_change_requests == [account_id]
    StartupReconciliation(repository, manager, funpay).run(now + timedelta(minutes=16))
    assert repository.get_operation(rotation.id).status == OperationStatus.FAILED
    assert repository.get_account(account_id).status == AccountStatus.MANUAL_REVIEW
    assert repository.get_rental(started.rental_id).status == RentalStatus.MANUAL_REVIEW
    assert pixel.password_change_requests == [account_id]
    assert not funpay.get_lot_state(repository.account_lot_ids(account_id)[0])


def test_password_wait_restart_at_six_minutes_then_delayed_email_resumes_same_operation(core, now):
    repository, manager, funpay, _gaijin = core
    account_id = create_test_account(repository, funpay, "PS-WAIT-RESUME", now)
    pixel, secrets = _pixel_manager(repository, manager, account_id)
    events = ClassifiedEmailRepository(repository.db)
    ephemeral = FakeEphemeralEmailSecretStore()
    manager._pixelstorm_security = PixelStormSecurityService(  # type: ignore[attr-defined]
        pixel,
        secrets,
        repository=repository,
        password_rotator=PasswordRotator(events, ephemeral),
    )
    started = manager.accept_order(OrderInput("wait-resume", "buyer", "6H", 1), now)
    assert started.rental_id is not None
    manager.run_operations(now)
    manager.run_operations(now)
    repository.expire_due(now + timedelta(seconds=2))
    pixel.require_password_email_confirmation(account_id)
    manager.run_operations(now + timedelta(seconds=2))
    manager.run_operations(now + timedelta(seconds=2))
    rotation = _operation(repository, OperationKind.ROTATE_PASSWORD)
    assert rotation is not None and rotation.security_state == "WAITING_PASSWORD_CHANGE_EMAIL"
    StartupReconciliation(repository, manager, funpay).run(now + timedelta(minutes=6))
    assert repository.get_operation(rotation.id).status == OperationStatus.RUNNING
    event = ClassifiedEmailEvent(
        "wait-resume-reset",
        "wait-resume-reset-mail",
        EmailMessageType.PASSWORD_CHANGE,
        now + timedelta(minutes=7),
        account_id,
        rotation.id,
        EmailPayloadState.AVAILABLE,
    )
    assert events.store_event(event, now + timedelta(minutes=7))
    assert ephemeral.put(
        event.id,
        "https://login.pixstorm.ru/ru/sso/changePassword/000000?token=SAFE",
        expires_at=now + timedelta(minutes=8),
    )
    manager.run_operations(now + timedelta(minutes=7))
    assert repository.get_operation(rotation.id).status == OperationStatus.COMPLETED
    assert pixel.password_change_requests == [account_id]


def test_maintenance_otp_wait_has_own_deadline(core, now):
    repository, manager, funpay, _gaijin = core
    account_id = create_test_account(repository, funpay, "PS-OTP-DEADLINE", now)
    pixel, secrets = _pixel_manager(repository, manager, account_id)
    events = ClassifiedEmailRepository(repository.db)
    manager._pixelstorm_security = PixelStormSecurityService(  # type: ignore[attr-defined]
        pixel,
        secrets,
        repository=repository,
        maintenance_otp=PixelStormMaintenanceOtpService(events, FakeEphemeralEmailSecretStore()),
    )
    started = manager.accept_order(OrderInput("otp-deadline", "buyer", "6H", 1), now)
    assert started.rental_id is not None
    manager.run_operations(now)
    manager.run_operations(now)
    repository.expire_due(now + timedelta(seconds=2))
    pixel.set_health(account_id, PixelStormHealth.AUTH_REQUIRED)
    pixel.set_auth_results(account_id, [PixelStormAuthResult.EMAIL_OTP_REQUIRED])
    manager.run_operations(now + timedelta(seconds=2))
    revoke = _operation(repository, OperationKind.REVOKE_SESSIONS)
    assert revoke is not None and revoke.security_state == "WAITING_LOGIN_OTP"
    StartupReconciliation(repository, manager, funpay).run(now + timedelta(minutes=4))
    assert repository.get_operation(revoke.id).status == OperationStatus.RUNNING
    StartupReconciliation(repository, manager, funpay).run(now + timedelta(minutes=6))
    assert repository.get_operation(revoke.id).status == OperationStatus.FAILED
    assert repository.get_account(account_id).status == AccountStatus.MANUAL_REVIEW


@pytest.mark.parametrize(
    ("password_change", "expected_state"),
    [(False, "WAITING_LOGIN_OTP"), (True, "WAITING_PASSWORD_CHANGE_EMAIL")],
)
def test_startup_restart_preserves_waiting_external_and_normal_worker_resumes(
    tmp_path, now, password_change, expected_state
):
    path, _old_repo, funpay, account_id, secrets, events, ephemeral, operation, rental_id = _file_waiting_operation(
        tmp_path, now, password_change=password_change
    )
    fresh_repo = Repository(Database(f"sqlite:///{path.as_posix()}"))
    fresh_pixel = FakePixelStormAdapter()
    fresh_pixel.set_credentials(account_id, "restart-login", "restart-current")
    fresh_service = PixelStormSecurityService(
        fresh_pixel,
        secrets,
        repository=fresh_repo,
        maintenance_otp=PixelStormMaintenanceOtpService(events, ephemeral),
        password_rotator=PasswordRotator(events, ephemeral),
    )
    fresh_manager = RentalManager(fresh_repo, funpay, None, secrets, pixelstorm_security=fresh_service)
    StartupReconciliation(fresh_repo, fresh_manager, funpay).run(now + timedelta(seconds=3))
    durable = fresh_repo.get_operation(operation.id)
    assert durable.status == OperationStatus.RUNNING
    assert durable.security_state == expected_state
    assert fresh_repo.get_account(account_id).status != AccountStatus.MANUAL_REVIEW
    assert fresh_repo.get_rental(rental_id).status != RentalStatus.MANUAL_REVIEW

    event_type = EmailMessageType.PASSWORD_CHANGE if password_change else EmailMessageType.LOGIN_OTP
    event = ClassifiedEmailEvent(
        f"restart-{expected_state}",
        f"restart-{expected_state}-mail",
        event_type,
        now + timedelta(seconds=4),
        account_id,
        durable.id if password_change else None,
        EmailPayloadState.AVAILABLE,
    )
    assert events.store_event(event, now + timedelta(seconds=4))
    payload = "https://login.pixstorm.ru/ru/sso/changePassword/000000?token=RESET" if password_change else "OTP"
    assert ephemeral.put(event.id, payload, expires_at=now + timedelta(minutes=2))
    if password_change:
        fresh_pixel.require_password_email_confirmation(account_id)
    else:
        fresh_pixel.set_health(account_id, PixelStormHealth.AUTH_REQUIRED)
        fresh_pixel.set_auth_results(account_id, [PixelStormAuthResult.SUCCESS])
    fresh_manager.run_operations(now + timedelta(seconds=4))
    assert fresh_repo.get_operation(durable.id).status == OperationStatus.COMPLETED


def test_stale_startup_recovery_owner_cannot_finalize_after_reclaim(tmp_path, now):
    database = Database(f"sqlite:///{(tmp_path / 'cas-recovery.db').as_posix()}")
    database.create_schema()
    repo = Repository(database)
    account_id = repo.add_account("PS-CAS", now)
    repo.add_account_lot(account_id, "lot-cas", now)
    result = repo.reserve_order(OrderInput("cas", "buyer", "6H", 1), now)
    assert result.rental_id is not None
    operation = repo.pending_operations()[0]
    assert repo.claim_operation(operation.id, now)
    first = repo.claim_startup_recovery(operation.id, now + timedelta(seconds=31))
    assert first is not None and first.recovery_claim_token is not None
    second = repo.claim_startup_recovery(operation.id, now + timedelta(minutes=6))
    assert second is not None and second.recovery_claim_token is not None
    assert first.recovery_claim_token != second.recovery_claim_token
    assert not repo.complete_recovery_operation(operation.id, first.recovery_claim_token, now + timedelta(minutes=6))
    assert not repo.fail_recovery_operation(operation.id, first.recovery_claim_token, now + timedelta(minutes=6))
    still_owned = repo.get_operation(operation.id)
    assert still_owned.status == OperationStatus.RUNNING
    assert still_owned.recovery_claim_token == second.recovery_claim_token
    assert repo.get_account(account_id).credential_version == 1
    assert repo.complete_recovery_operation(operation.id, second.recovery_claim_token, now + timedelta(minutes=6))
    assert repo.get_operation(operation.id).status == OperationStatus.COMPLETED


def test_revoke_capability_and_crash_recovery_without_repeat(core, now):
    repository, manager, funpay, _gaijin = core
    account_id = create_test_account(repository, funpay, "PS02", now)
    notifier = FakeOwnerNotifier()
    pixel, _secrets = _pixel_manager(repository, manager, account_id, notifier=notifier)
    result = manager.accept_order(OrderInput("ps-order", "buyer", "6H", 1), now)
    manager.run_operations(now)
    manager.run_operations(now)
    repository.expire_due(now + timedelta(seconds=2))
    revoke = _operation(repository, OperationKind.REVOKE_SESSIONS)
    assert revoke is not None
    claimed = repository.claim_operation(revoke.id, now + timedelta(seconds=2))
    assert claimed is not None
    assert manager._pixelstorm_security.revoke(account_id, revoke.id, now + timedelta(seconds=2))  # type: ignore[attr-defined]
    assert pixel.revoke_calls == [account_id]
    StartupReconciliation(repository, manager, funpay).run(now + timedelta(seconds=33))
    assert pixel.revoke_calls == [account_id]
    assert _operation(repository, OperationKind.ROTATE_PASSWORD).status == OperationStatus.PENDING
    pixel.set_capabilities(account_id, PixelStormSecurityCapabilities(False, False, False, True))
    assert not manager._pixelstorm_security.revoke(account_id, "unsupported", now)  # type: ignore[attr-defined]
    assert notifier.notifications[-1].category == "SESSION_REVOCATION_UNSUPPORTED"
    pixel.set_capabilities(account_id, PixelStormSecurityCapabilities(True, True, False, True))
    pixel.set_revocation_result(account_id, PixelStormRevocationResult.AMBIGUOUS)
    assert not manager._pixelstorm_security.revoke(account_id, "ambiguous", now)  # type: ignore[attr-defined]
    assert notifier.notifications[-1].category == "SESSION_REVOCATION_AMBIGUOUS"
    assert result.rental_id is not None


def test_rotation_pending_recovery_and_ambiguous_fail_closed(core, now):
    repository, manager, funpay, _gaijin = core
    account_id = create_test_account(repository, funpay, "PS03", now)
    pixel, secrets = _pixel_manager(repository, manager, account_id)
    current = secrets.get_current_credentials(account_id)
    assert current
    secrets.set_pending_credentials(account_id, current[0], "UNIQUE_PENDING_PASSWORD_SECRET")
    pixel.set_credentials(account_id, current[0], "UNIQUE_PENDING_PASSWORD_SECRET")
    assert manager._pixelstorm_security.rotate(account_id, "rotation", now, recovery=True)  # type: ignore[attr-defined]
    assert secrets.get_current_credentials(account_id)[1] == "UNIQUE_PENDING_PASSWORD_SECRET"  # type: ignore[index]
    assert pixel.rotation_calls == []
    pixel.set_health(account_id, PixelStormHealth.CHALLENGE)
    assert not manager._pixelstorm_security.rotate(account_id, "challenge", now)  # type: ignore[attr-defined]


def test_rotation_uses_same_pending_and_both_invalid_fails_closed(core, now):
    repository, manager, funpay, _gaijin = core
    account_id = create_test_account(repository, funpay, "PS03B", now)
    pixel, secrets = _pixel_manager(repository, manager, account_id)
    current = secrets.get_current_credentials(account_id)
    assert current is not None
    secrets.set_pending_credentials(account_id, current[0], "UNIQUE_PENDING_PASSWORD_SECRET")
    assert manager._pixelstorm_security.rotate(account_id, "rotation", now)  # type: ignore[attr-defined]
    assert pixel.rotation_calls == [account_id]
    promoted = secrets.get_current_credentials(account_id)
    assert promoted is not None and promoted[1] != current[1]
    assert pixel.verify_credentials(account_id, *current) == PixelStormCredentialResult.INVALID
    assert pixel.verify_credentials(account_id, *promoted) == PixelStormCredentialResult.VALID
    secrets.set_pending_credentials(account_id, current[0], "UNIQUE_PENDING_PASSWORD_SECRET")
    pixel.set_credentials(account_id, current[0], "different")
    assert not manager._pixelstorm_security.rotate(account_id, "both-invalid", now, recovery=True)  # type: ignore[attr-defined]
    assert pixel.rotation_calls == [account_id]


def test_rotation_crash_after_claim_before_pending_generation_rotates_on_recovery(core, now):
    repository, manager, funpay, _gaijin = core
    account_id = create_test_account(repository, funpay, "PS-PRE-PENDING", now)
    pixel, _secrets = _pixel_manager(repository, manager, account_id)
    result = manager.accept_order(OrderInput("pre-pending", "buyer", "6H", 1), now)
    manager.run_operations(now)
    manager.run_operations(now)
    repository.expire_due(now + timedelta(seconds=2))
    manager.run_operations(now + timedelta(seconds=2))
    rotation = _operation(repository, OperationKind.ROTATE_PASSWORD)
    assert rotation is not None and repository.claim_operation(rotation.id, now + timedelta(seconds=2))
    # Simulated crash here: no pending credential and remote is still OLD.
    assert manager._pixelstorm_security.rotate(account_id, rotation.id, now + timedelta(seconds=3), recovery=True)  # type: ignore[attr-defined]
    repository.operation_completed(rotation.id, now + timedelta(seconds=3))
    assert pixel.rotation_calls == [account_id]
    assert repository.get_account(account_id).credential_version == 2
    assert result.rental_id is not None


def test_new_worker_recovers_revoke_and_post_promotion_rotation(tmp_path, now):
    app_path = tmp_path / "application.db"
    remote = PersistentFakePixelStormBackend(str(tmp_path / "remote.db"))
    vault_id = str(tmp_path / "secure-vault")
    database = Database(f"sqlite:///{app_path.as_posix()}")
    database.create_schema()
    repo1 = Repository(database)
    core_funpay = FakeFunPayAdapter()
    account_id = repo1.add_account("PS-RESTART", now)
    repo1.add_account_lot(account_id, "lot-restart", now)
    core_funpay.set_lot_state("lot-restart", enabled=True)
    secrets1 = PersistentFakeSecureStore(vault_id)
    secrets1.set_current_credentials(account_id, "UNIQUE_PIXELSTORM_LOGIN_SECRET", "UNIQUE_CURRENT_PASSWORD_SECRET")
    pixel1 = FakePixelStormAdapter(remote)
    pixel1.set_credentials(account_id, "UNIQUE_PIXELSTORM_LOGIN_SECRET", "UNIQUE_CURRENT_PASSWORD_SECRET")
    manager1 = RentalManager(
        repo1,
        core_funpay,
        None,
        secrets1,
        pixelstorm_security=PixelStormSecurityService(pixel1, secrets1, repository=repo1),
    )
    started = manager1.accept_order(OrderInput("restart", "buyer", "6H", 1), now)
    manager1.run_operations(now)
    manager1.run_operations(now)
    repo1.expire_due(now + timedelta(seconds=2))
    revoke = _operation(repo1, OperationKind.REVOKE_SESSIONS)
    assert revoke is not None and repo1.claim_operation(revoke.id, now + timedelta(seconds=2))
    assert repo1.prepare_operation(revoke.id, now + timedelta(seconds=2))
    assert manager1._pixelstorm_security.revoke(account_id, revoke.id, now + timedelta(seconds=2))  # type: ignore[attr-defined]
    assert remote.counter(account_id, "revoke_count") == 1
    database.engine.dispose()

    # Fresh repository, adapter and secure-store objects emulate a new worker.
    repo2 = Repository(Database(f"sqlite:///{app_path.as_posix()}"))
    pixel2 = FakePixelStormAdapter(remote)
    secrets2 = PersistentFakeSecureStore(vault_id)
    manager2 = RentalManager(
        repo2,
        core_funpay,
        None,
        secrets2,
        pixelstorm_security=PixelStormSecurityService(pixel2, secrets2, repository=repo2),
    )
    assert pixel1 is not pixel2
    StartupReconciliation(repo2, manager2, core_funpay).run(now + timedelta(seconds=33))
    assert remote.counter(account_id, "revoke_count") == 1
    rotation = _operation(repo2, OperationKind.ROTATE_PASSWORD)
    assert rotation is not None and repo2.claim_operation(rotation.id, now + timedelta(seconds=33))
    assert manager2._pixelstorm_security.rotate(account_id, rotation.id, now + timedelta(seconds=33))  # type: ignore[attr-defined]
    assert remote.counter(account_id, "rotation_count") == 1

    repo3 = Repository(Database(f"sqlite:///{app_path.as_posix()}"))
    pixel3 = FakePixelStormAdapter(remote)
    manager3 = RentalManager(
        repo3,
        core_funpay,
        None,
        PersistentFakeSecureStore(vault_id),
        pixelstorm_security=PixelStormSecurityService(pixel3, PersistentFakeSecureStore(vault_id), repository=repo3),
    )
    StartupReconciliation(repo3, manager3, core_funpay).run(now + timedelta(seconds=334))
    assert remote.counter(account_id, "rotation_count") == 1
    assert repo3.get_account(account_id).credential_version == 2
    assert started.rental_id is not None


def test_multiprocess_revoke_has_one_durable_owner(tmp_path, now):
    app_path = tmp_path / "multiprocess.db"
    remote_path = tmp_path / "multiprocess-remote.db"
    remote = PersistentFakePixelStormBackend(str(remote_path))
    database = Database(f"sqlite:///{app_path.as_posix()}")
    database.create_schema()
    repo = Repository(database)
    funpay = FakeFunPayAdapter()
    account_id = repo.add_account("PS-MP", now)
    repo.add_account_lot(account_id, "lot-mp", now)
    funpay.set_lot_state("lot-mp", enabled=True)
    secrets = FakeSecureStore()
    secrets.set_current_credentials(account_id, "login", "password")
    pixel = FakePixelStormAdapter(remote)
    pixel.set_credentials(account_id, "login", "password")
    manager = RentalManager(repo, funpay, None, secrets, pixelstorm_security=PixelStormSecurityService(pixel, secrets, repository=repo))
    manager.accept_order(OrderInput("mp", "buyer", "6H", 1), now)
    manager.run_operations(now)
    manager.run_operations(now)
    repo.expire_due(now + timedelta(seconds=2))
    context = get_context("spawn")
    with context.Pool(2) as pool:
        outcomes = pool.starmap(_revoke_worker, [(str(app_path), str(remote_path), account_id, now + timedelta(seconds=2))] * 2)
    assert outcomes.count(True) == 1
    assert remote.counter(account_id, "revoke_count") == 1


def test_multiprocess_rotation_has_one_durable_owner(tmp_path, now):
    app_path, remote_path, vault_path = tmp_path / "rotate.db", tmp_path / "rotate-remote.db", tmp_path / "rotate-vault.db"
    remote = PersistentFakePixelStormBackend(str(remote_path))
    database = Database(f"sqlite:///{app_path.as_posix()}")
    database.create_schema()
    repo = Repository(database)
    funpay = FakeFunPayAdapter()
    account_id = repo.add_account("PS-MP-ROTATE", now)
    repo.add_account_lot(account_id, "lot-mp-rotate", now)
    funpay.set_lot_state("lot-mp-rotate", enabled=True)
    secrets = PersistentFakeSecureStore(str(vault_path))
    secrets.set_current_credentials(account_id, "old-login", "OLD")
    remote_adapter = FakePixelStormAdapter(remote)
    remote_adapter.set_credentials(account_id, "old-login", "OLD")
    manager = RentalManager(repo, funpay, None, secrets, pixelstorm_security=PixelStormSecurityService(remote_adapter, secrets, repository=repo))
    manager.accept_order(OrderInput("mp-rotate", "buyer", "6H", 1), now)
    manager.run_operations(now)
    manager.run_operations(now)
    repo.expire_due(now + timedelta(seconds=2))
    manager.run_operations(now + timedelta(seconds=2))
    context = get_context("spawn")
    with context.Pool(2) as pool:
        outcomes = pool.starmap(_rotate_worker, [(str(app_path), str(remote_path), str(vault_path), account_id, now + timedelta(seconds=3))] * 2)
    assert outcomes.count(True) == 1
    assert remote.counter(account_id, "rotation_count") == 1
    assert secrets.pending_created_count(account_id) == 1
    assert repo.get_account(account_id).credential_version == 2
    assert _operation(repo, OperationKind.ROTATE_PASSWORD).status == OperationStatus.COMPLETED


@pytest.mark.parametrize("kind", [OperationKind.REVOKE_SESSIONS, OperationKind.ROTATE_PASSWORD])
def test_multiprocess_startup_recovery_has_one_durable_owner(tmp_path, now, kind):
    app_path = tmp_path / f"startup-{kind}.db"
    remote_path = tmp_path / f"startup-{kind}-remote.db"
    vault_path = tmp_path / f"startup-{kind}-vault.db"
    database = Database(f"sqlite:///{app_path.as_posix()}")
    database.create_schema()
    repo = Repository(database)
    funpay = FakeFunPayAdapter()
    account_id = repo.add_account(f"PS-STARTUP-{kind}", now)
    repo.add_account_lot(account_id, "lot-startup", now)
    funpay.set_lot_state("lot-startup", enabled=True)
    secrets = PersistentFakeSecureStore(str(vault_path))
    secrets.set_current_credentials(account_id, "startup-login", "startup-current")
    remote = PersistentFakePixelStormBackend(str(remote_path))
    pixel = FakePixelStormAdapter(remote)
    pixel.set_credentials(account_id, "startup-login", "startup-current")
    manager = RentalManager(
        repo,
        funpay,
        None,
        secrets,
        pixelstorm_security=PixelStormSecurityService(pixel, secrets, repository=repo),
    )
    result = manager.accept_order(OrderInput(f"startup-{kind}", "buyer", "6H", 1), now)
    assert result.rental_id is not None
    manager.run_operations(now)
    manager.run_operations(now)
    repo.expire_due(now + timedelta(seconds=2))
    revoke = _operation(repo, OperationKind.REVOKE_SESSIONS)
    assert revoke is not None and repo.claim_operation(revoke.id, now + timedelta(seconds=2))
    assert repo.prepare_operation(revoke.id, now + timedelta(seconds=2))
    if kind == OperationKind.REVOKE_SESSIONS:
        # Simulated dead process completed the external action, but not DB finalization.
        assert manager._pixelstorm_security.revoke(account_id, revoke.id, now + timedelta(seconds=2))  # type: ignore[attr-defined]
        operation = revoke
    else:
        assert repo.operation_completed(revoke.id, now + timedelta(seconds=2))
        operation = _operation(repo, OperationKind.ROTATE_PASSWORD)
        assert operation is not None and repo.claim_operation(operation.id, now + timedelta(seconds=2))

    context = get_context("spawn")
    with context.Pool(2) as pool:
        outcomes = pool.starmap(
            _startup_security_worker,
            [(str(app_path), str(remote_path), str(vault_path), now + timedelta(seconds=33))] * 2,
        )
    assert all(outcomes)
    with repo.db.session() as session:
        claims = list(
            session.scalars(
                select(AuditEventRow).where(
                    AuditEventRow.event_type == "STARTUP_RECOVERY_CLAIMED",
                    AuditEventRow.correlation_id.like(f"{operation.id}:%"),
                )
            )
        )
    assert len(claims) == 1
    assert repo.get_operation(operation.id).status == OperationStatus.COMPLETED
    if kind == OperationKind.REVOKE_SESSIONS:
        assert remote.counter(account_id, "revoke_count") == 1
    else:
        assert remote.counter(account_id, "rotation_count") == 1
        assert repo.get_account(account_id).credential_version == 2


def test_multiprocess_recovery_claim_crash_then_reclaim_rejects_old_owner(tmp_path, now):
    database = Database(f"sqlite:///{(tmp_path / 'process-reclaim.db').as_posix()}")
    database.create_schema()
    repo = Repository(database)
    account_id = repo.add_account("PS-PROCESS-RECLAIM", now)
    repo.add_account_lot(account_id, "lot-process-reclaim", now)
    result = repo.reserve_order(OrderInput("process-reclaim", "buyer", "6H", 1), now)
    assert result.rental_id is not None
    operation = repo.pending_operations()[0]
    assert repo.claim_operation(operation.id, now)
    context = get_context("spawn")
    with context.Pool(1) as pool:
        old_token = pool.starmap(
            _startup_claim_only_worker,
            [(str(tmp_path / "process-reclaim.db"), operation.id, now + timedelta(seconds=31))],
        )[0]
    assert old_token is not None
    new_owner = repo.claim_startup_recovery(operation.id, now + timedelta(minutes=6))
    assert new_owner is not None and new_owner.recovery_claim_token is not None
    assert not repo.complete_recovery_operation(operation.id, old_token, now + timedelta(minutes=6))
    assert repo.complete_recovery_operation(operation.id, new_owner.recovery_claim_token, now + timedelta(minutes=6))


@pytest.mark.parametrize("kind", [OperationKind.REVOKE_SESSIONS, OperationKind.ROTATE_PASSWORD])
def test_multiprocess_startup_cannot_steal_active_normal_worker_lease(tmp_path, now, kind):
    app_path = tmp_path / f"active-lease-{kind}.db"
    remote_path = tmp_path / f"active-lease-{kind}-remote.db"
    vault_path = tmp_path / f"active-lease-{kind}-vault.db"
    database = Database(f"sqlite:///{app_path.as_posix()}")
    database.create_schema()
    repo = Repository(database)
    funpay = FakeFunPayAdapter()
    account_id = repo.add_account(f"PS-ACTIVE-{kind}", now)
    repo.add_account_lot(account_id, "lot-active-lease", now)
    funpay.set_lot_state("lot-active-lease", enabled=True)
    secrets = PersistentFakeSecureStore(str(vault_path))
    secrets.set_current_credentials(account_id, "active-login", "active-current")
    remote = PersistentFakePixelStormBackend(str(remote_path))
    pixel = FakePixelStormAdapter(remote)
    pixel.set_credentials(account_id, "active-login", "active-current")
    service = PixelStormSecurityService(pixel, secrets, repository=repo)
    manager = RentalManager(repo, funpay, None, secrets, pixelstorm_security=service)
    started = manager.accept_order(OrderInput(f"active-{kind}", "buyer", "6H", 1), now)
    assert started.rental_id is not None
    manager.run_operations(now)
    manager.run_operations(now)
    repo.expire_due(now + timedelta(seconds=2))
    revoke = _operation(repo, OperationKind.REVOKE_SESSIONS)
    assert revoke is not None and repo.claim_operation(revoke.id, now + timedelta(seconds=2))
    assert repo.prepare_operation(revoke.id, now + timedelta(seconds=2))
    if kind == OperationKind.REVOKE_SESSIONS:
        operation = revoke
    else:
        assert service.revoke(account_id, revoke.id, now + timedelta(seconds=2))
        assert repo.operation_completed(revoke.id, now + timedelta(seconds=2))
        operation = _operation(repo, OperationKind.ROTATE_PASSWORD)
        assert operation is not None and repo.claim_operation(operation.id, now + timedelta(seconds=2))
    context = get_context("spawn")
    with context.Pool(1) as pool:
        pool.starmap(
            _startup_security_worker,
            [(str(app_path), str(remote_path), str(vault_path), now + timedelta(seconds=3))],
        )
    durable = repo.get_operation(operation.id)
    assert durable.status == OperationStatus.RUNNING
    assert durable.recovery_claim_token is None
    assert remote.counter(account_id, "revoke_count" if kind == OperationKind.REVOKE_SESSIONS else "rotation_count") == 0
    if kind == OperationKind.REVOKE_SESSIONS:
        assert service.revoke(account_id, operation.id, now + timedelta(seconds=3))
    else:
        assert service.rotate(account_id, operation.id, now + timedelta(seconds=3))
    assert repo.operation_completed(operation.id, now + timedelta(seconds=3))
    assert remote.counter(account_id, "revoke_count" if kind == OperationKind.REVOKE_SESSIONS else "rotation_count") == 1


def test_startup_recovery_claim_can_be_reclaimed_after_owner_crash(tmp_path, now):
    database = Database(f"sqlite:///{(tmp_path / 'reclaim.db').as_posix()}")
    database.create_schema()
    repo1 = Repository(database)
    account_id = repo1.add_account("PS-RECLAIM", now)
    repo1.add_account_lot(account_id, "lot-reclaim", now)
    result = repo1.reserve_order(OrderInput("reclaim", "buyer", "6H", 1), now)
    assert result.rental_id is not None
    operation = repo1.pending_operations()[0]
    assert repo1.claim_operation(operation.id, now)
    first = repo1.claim_startup_recovery(operation.id, now + timedelta(seconds=31))
    assert first is not None
    repo2 = Repository(Database(f"sqlite:///{(tmp_path / 'reclaim.db').as_posix()}"))
    assert repo2.claim_startup_recovery(operation.id, now + timedelta(minutes=6)) is not None


def test_secret_markers_never_reach_application_sqlite_or_owner_notifications(tmp_path, now):
    markers = [
        "UNIQUE_PIXELSTORM_LOGIN_SECRET_41A7", "UNIQUE_CURRENT_PASSWORD_SECRET_52B8",
        "UNIQUE_PENDING_PASSWORD_SECRET_63C9", "UNIQUE_PIXELSTORM_OTP_SECRET_74D0",
        "UNIQUE_PIXELSTORM_SESSION_SECRET_85E1", "UNIQUE_RESET_TOKEN_SECRET_96F2",
    ]
    path = tmp_path / "application.db"
    database = Database(f"sqlite:///{path.as_posix()}")
    database.create_schema()
    repo = Repository(database)
    account_id = repo.add_account("PS-SECRETS", now)
    notifier = FakeOwnerNotifier()
    secrets = FakeSecureStore()
    secrets.set_current_credentials(account_id, markers[0], markers[1])
    secrets.set_pending_credentials(account_id, markers[0], markers[2])
    secrets.set_pixelstorm_session(account_id, markers[4])
    ephemeral = FakeEphemeralEmailSecretStore()
    assert ephemeral.put("otp", markers[3], expires_at=now + timedelta(minutes=1))
    assert ephemeral.put("reset", f"https://login.pixstorm.ru/ru/sso/changePassword/000000?token={markers[5]}", expires_at=now + timedelta(minutes=1))
    notifier.notify(OwnerNotification("PIXEL_STORM_CHALLENGE", "safe-operation", now, account_id=account_id))
    database.engine.dispose()
    durable = path.read_bytes().decode("latin1")
    assert all(marker not in durable for marker in markers)
    assert all(all(marker not in str(value) for marker in markers) for value in notifier.notifications)


def test_secret_markers_are_redacted_through_actual_maintenance_and_rotation_workflow(
    tmp_path, now, caplog
):
    markers = [
        "UNIQUE_PIXELSTORM_LOGIN_SECRET_41A7",
        "UNIQUE_CURRENT_PASSWORD_SECRET_52B8",
        "UNIQUE_PENDING_PASSWORD_SECRET_63C9",
        "UNIQUE_PIXELSTORM_OTP_SECRET_74D0",
        "UNIQUE_PIXELSTORM_SESSION_SECRET_85E1",
        "UNIQUE_RESET_TOKEN_SECRET_96F2",
    ]
    path = tmp_path / "marker-workflow.db"
    database = Database(f"sqlite:///{path.as_posix()}")
    database.create_schema()
    repo = Repository(database)
    funpay = FakeFunPayAdapter()
    account_id = repo.add_account("PS-MARKERS", now)
    repo.add_account_lot(account_id, "lot-markers", now)
    funpay.set_lot_state("lot-markers", enabled=True)
    secrets = FakeSecureStore()
    secrets.set_current_credentials(account_id, markers[0], markers[1])
    secrets.set_pixelstorm_session(account_id, markers[4])
    notifier = FakeOwnerNotifier()
    pixel = FakePixelStormAdapter()
    pixel.set_credentials(account_id, markers[0], markers[1])
    events = ClassifiedEmailRepository(database)
    ephemeral = FakeEphemeralEmailSecretStore()
    service = PixelStormSecurityService(
        pixel,
        secrets,
        notifier,
        repository=repo,
        maintenance_otp=PixelStormMaintenanceOtpService(events, ephemeral),
        password_generator=_DeterministicPasswordGenerator(),
        password_rotator=PasswordRotator(events, ephemeral),
    )
    manager = RentalManager(repo, funpay, None, secrets, notifier, pixelstorm_security=service)
    started = manager.accept_order(OrderInput("marker-workflow", "buyer", "6H", 1), now)
    assert started.rental_id is not None
    manager.run_operations(now)
    manager.run_operations(now)
    repo.expire_due(now + timedelta(seconds=2))
    pixel.set_health(account_id, PixelStormHealth.AUTH_REQUIRED)
    pixel.set_auth_results(account_id, [PixelStormAuthResult.EMAIL_OTP_REQUIRED, PixelStormAuthResult.SUCCESS])
    manager.run_operations(now + timedelta(seconds=2))
    revoke = _operation(repo, OperationKind.REVOKE_SESSIONS)
    assert revoke is not None and revoke.security_state == "WAITING_LOGIN_OTP"
    otp_event = ClassifiedEmailEvent(
        "marker-otp", "marker-otp-mail", EmailMessageType.LOGIN_OTP, now + timedelta(seconds=3), account_id, None, EmailPayloadState.AVAILABLE
    )
    assert events.store_event(otp_event, now + timedelta(seconds=3))
    assert ephemeral.put(otp_event.id, markers[3], expires_at=now + timedelta(minutes=1))
    manager.run_operations(now + timedelta(seconds=3))
    assert repo.get_operation(revoke.id).status == OperationStatus.COMPLETED

    pixel.require_password_email_confirmation(account_id)
    manager.run_operations(now + timedelta(seconds=3))
    rotation = _operation(repo, OperationKind.ROTATE_PASSWORD)
    assert rotation is not None and rotation.security_state == "WAITING_PASSWORD_CHANGE_EMAIL"
    reset_event = ClassifiedEmailEvent(
        "marker-reset", "marker-reset-mail", EmailMessageType.PASSWORD_CHANGE, now + timedelta(seconds=4), account_id, rotation.id, EmailPayloadState.AVAILABLE
    )
    assert events.store_event(reset_event, now + timedelta(seconds=4))
    reset_url = f"https://login.pixstorm.ru/ru/sso/changePassword/000000?token={markers[5]}"
    assert ephemeral.put(reset_event.id, reset_url, expires_at=now + timedelta(minutes=1))
    manager.run_operations(now + timedelta(seconds=4))
    assert repo.get_operation(rotation.id).status == OperationStatus.COMPLETED
    assert secrets.get_current_credentials(account_id) == (markers[0], markers[2])

    # Controlled failure reaches the owner path without serialising credentials.
    pixel.set_health(account_id, PixelStormHealth.CHALLENGE)
    assert not service.revoke(account_id, "marker-controlled-failure", now + timedelta(seconds=5))
    database.engine.dispose()
    durable = path.read_bytes().decode("latin1")
    assert all(marker not in durable for marker in markers)
    assert all(marker not in caplog.text for marker in markers)
    assert all(all(marker not in str(value) for marker in markers) for value in notifier.notifications)
    assert not list((tmp_path / "playwright-artifacts").glob("**/*"))


def test_full_pixelstorm_expiry_lifecycle_and_security_alert(core, now):
    repository, manager, funpay, _gaijin = core
    account_id = create_test_account(repository, funpay, "PS04", now)
    pixel, _secrets = _pixel_manager(repository, manager, account_id)
    result = manager.accept_order(OrderInput("ps-e2e", "buyer", "6H", 1), now)
    manager.run_operations(now)
    manager.run_operations(now)
    repository.expire_due(now + timedelta(seconds=2))
    manager.run_operations(now + timedelta(seconds=2))
    manager.run_operations(now + timedelta(seconds=2))
    manager.run_operations(now + timedelta(seconds=2))
    assert repository.get_account(account_id).status == AccountStatus.AVAILABLE
    assert repository.get_rental(result.rental_id).status == RentalStatus.FINISHED  # type: ignore[arg-type]
    assert pixel.revoke_calls == [account_id] and len(pixel.rotation_calls) == 1
    # New active rental receives an unexpected security event: it remains fail-closed and queues recovery.
    result2 = manager.accept_order(OrderInput("ps-alert", "buyer2", "6H", 100), now + timedelta(seconds=3))
    manager.run_operations(now + timedelta(seconds=3))
    manager.run_operations(now + timedelta(seconds=3))
    assert repository.record_active_security_event(account_id, "PASSWORD_CHANGE", "safe", now + timedelta(seconds=4))
    assert repository.get_account(account_id).status == AccountStatus.SECURITY_ALERT
    assert _operation(repository, OperationKind.REVOKE_SESSIONS).status == OperationStatus.PENDING
    manager.run_operations(now + timedelta(seconds=4))
    manager.run_operations(now + timedelta(seconds=4))
    manager.run_operations(now + timedelta(seconds=4))
    assert repository.get_account(account_id).status == AccountStatus.AVAILABLE
    assert repository.get_rental(result2.rental_id).status == RentalStatus.FINISHED  # type: ignore[arg-type]
    assert result2.rental_id is not None


def test_maintenance_otp_correlation_one_time_and_secret_absence(core, now):
    repository, manager, funpay, _gaijin = core
    account_id = create_test_account(repository, funpay, "PS05", now)
    result = manager.accept_order(OrderInput("ps-otp", "buyer", "6H", 10), now)
    manager.run_operations(now)
    manager.run_operations(now)
    repository.expire_due(now + timedelta(seconds=11))
    revoke = _operation(repository, OperationKind.REVOKE_SESSIONS)
    assert revoke
    assert repository.claim_operation(revoke.id, now + timedelta(seconds=11))
    events = ClassifiedEmailRepository(repository.db)
    stale = ClassifiedEmailEvent("old", "old-mail", EmailMessageType.LOGIN_OTP, now + timedelta(seconds=10), account_id, None, EmailPayloadState.AVAILABLE)
    fresh = ClassifiedEmailEvent("fresh", "fresh-mail", EmailMessageType.LOGIN_OTP, now + timedelta(seconds=12), account_id, None, EmailPayloadState.AVAILABLE)
    assert events.store_event(stale, now) and events.store_event(fresh, now)
    claim = events.claim_maintenance_login_otp(account_id, revoke.id, now + timedelta(seconds=11), now + timedelta(seconds=12))
    assert claim is not None and claim.event_id == "fresh"
    assert events.claim_maintenance_login_otp(account_id, revoke.id, now + timedelta(seconds=11), now + timedelta(seconds=12)) is None
    with repository.db.session() as session:
        persisted = " ".join(str(row) for row in session.scalars(select(AuditEventRow)).all()) + " ".join(str(row) for row in session.scalars(select(ClassifiedEmailEventRow)).all())
    assert "UNIQUE_PIXELSTORM" not in persisted
    assert result.rental_id is not None
