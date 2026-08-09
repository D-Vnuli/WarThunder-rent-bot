"""Playwright-only Pixel Storm browser boundary.

The page contract is intentionally synthetic until owner-gated read-only UI
validation confirms live URLs and selectors.  Browser tracing, video and
screenshots are disabled for sensitive contexts by default.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

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
from app.domain.ports import WebSessionStore

_ALLOWED_ORIGINS = {"https://login.pixstorm.ru", "https://warthunder.ru"}


class LocatorBoundary(Protocol):
    def count(self) -> int: ...
    def get_attribute(self, name: str) -> str | None: ...
    def fill(self, value: str) -> None: ...
    def click(self) -> None: ...


class PageBoundary(Protocol):
    @property
    def url(self) -> str: ...
    def locator(self, selector: str) -> LocatorBoundary: ...


@dataclass(frozen=True)
class PixelStormBrowserSafety:
    trace: str = "off"
    video: str = "off"
    screenshots: bool = False
    debug_artifacts_enabled: bool = False


class PixelStormBrowserContextFactory:
    """Creates sensitive contexts without persistent session or media artifacts."""

    def __init__(self, safety: PixelStormBrowserSafety | None = None) -> None:
        self.safety = safety or PixelStormBrowserSafety()

    def new_context(
        self,
        browser: Any,
        *,
        storage_state: dict[str, Any] | None = None,
        debug_artifact_dir: Path | None = None,
    ) -> Any:
        if debug_artifact_dir is not None and not self.safety.debug_artifacts_enabled:
            raise ValueError("Pixel Storm debug artifacts require explicit opt-in")
        # Deliberately omit storage_state and record_video_dir.  Tracing is not
        # started here and screenshots are never auto-captured by this boundary.
        options: dict[str, Any] = {}
        if storage_state is not None:
            # Playwright receives an in-memory mapping, never a file path.
            options["storage_state"] = storage_state
        if debug_artifact_dir is not None:
            debug_artifact_dir.mkdir(parents=True, exist_ok=True)
            options["record_video_dir"] = str(debug_artifact_dir)
        return browser.new_context(**options)


class PixelStormCredentialVerificationFactory(Protocol):
    """Creates a fresh, isolated login page for supplied-credential checks."""

    def open_verification_page(self, account_id: str) -> "PageBoundary | None": ...


class PixelStormBrowserSessionFactory:
    """Secure in-memory Playwright session boundary backed only by WebSessionStore."""

    def __init__(
        self,
        browser: Any,
        sessions: WebSessionStore,
        login_url: str,
        *,
        context_factory: PixelStormBrowserContextFactory | None = None,
        page_initializer: Callable[[Any, str], "PageBoundary"] | None = None,
    ) -> None:
        self._browser = browser
        self._sessions = sessions
        self._login_url = login_url
        self._contexts = context_factory or PixelStormBrowserContextFactory()
        self._page_initializer = page_initializer

    def open_account_page(self, account_id: str) -> "PageBoundary":
        serialized = self._sessions.get_pixelstorm_session(account_id)
        state: dict[str, Any] | None = None
        if serialized is not None:
            try:
                candidate = json.loads(serialized)
                if not isinstance(candidate, dict):
                    raise ValueError("storage state must be an object")
                state = candidate
            except (TypeError, ValueError, json.JSONDecodeError):
                self._sessions.clear_pixelstorm_session(account_id)
        context = self._contexts.new_context(self._browser, storage_state=state)
        return self._new_page(context)

    def open_verification_page(self, account_id: str) -> "PageBoundary":
        del account_id
        # Credentials are verified without account cookies or an existing page.
        return self._new_page(self._contexts.new_context(self._browser))

    def persist_page_session(self, account_id: str, page: "PageBoundary") -> None:
        context = getattr(page, "context", None)
        if context is None:
            return
        state = context.storage_state()
        self._sessions.set_pixelstorm_session(
            account_id, json.dumps(state, separators=(",", ":"), sort_keys=True)
        )

    def _new_page(self, context: Any) -> "PageBoundary":
        if self._page_initializer is not None:
            return self._page_initializer(context, self._login_url)
        page = context.new_page()
        page.goto(self._login_url)
        return page


@dataclass(frozen=True)
class PixelStormPageInspection:
    health: PixelStormHealth
    authentication: PixelStormAuthResult


class PixelStormPageObjects:
    """Selectors are limited to documented synthetic fixture contracts."""

    _contract_selector = "[data-pixelstorm-page]"

    @classmethod
    def inspect(cls, page: PageBoundary) -> PixelStormPageInspection:
        parsed = urlparse(page.url)
        origin = f"{parsed.scheme}://{parsed.hostname}" if parsed.hostname else ""
        if origin not in _ALLOWED_ORIGINS:
            return PixelStormPageInspection(PixelStormHealth.WRONG_REGION, PixelStormAuthResult.WRONG_REGION)
        locator = page.locator(cls._contract_selector)
        if locator.count() != 1:
            return PixelStormPageInspection(PixelStormHealth.UNKNOWN_UI, PixelStormAuthResult.UNKNOWN_UI)
        page_kind = locator.get_attribute("data-pixelstorm-page")
        mapping = {
            "authenticated": (PixelStormHealth.READY, PixelStormAuthResult.SUCCESS),
            "login": (PixelStormHealth.AUTH_REQUIRED, PixelStormAuthResult.BAD_CREDENTIALS),
            "expired": (PixelStormHealth.AUTH_REQUIRED, PixelStormAuthResult.BAD_CREDENTIALS),
            "otp": (PixelStormHealth.AUTH_REQUIRED, PixelStormAuthResult.EMAIL_OTP_REQUIRED),
            "pixel-pass": (PixelStormHealth.PIXEL_PASS_REQUIRED, PixelStormAuthResult.PIXEL_PASS_REQUIRED),
            "challenge": (PixelStormHealth.CHALLENGE, PixelStormAuthResult.CHALLENGE),
            "unknown": (PixelStormHealth.UNKNOWN_UI, PixelStormAuthResult.UNKNOWN_UI),
        }
        health, auth = mapping.get(
            page_kind or "", (PixelStormHealth.UNKNOWN_UI, PixelStormAuthResult.UNKNOWN_UI)
        )
        return PixelStormPageInspection(health, auth)

    @classmethod
    def submit_synthetic_login(
        cls, page: PageBoundary, login: str, password: str, otp: str | None = None
    ) -> PixelStormPageInspection:
        before = cls.inspect(page)
        if before.authentication == PixelStormAuthResult.EMAIL_OTP_REQUIRED:
            otp_input, submit = (
                page.locator("[data-pixelstorm-otp]"),
                page.locator("[data-pixelstorm-otp-submit]"),
            )
            if otp is None or otp_input.count() != 1 or submit.count() != 1:
                return PixelStormPageInspection(PixelStormHealth.UNKNOWN_UI, PixelStormAuthResult.UNKNOWN_UI)
            otp_input.fill(otp)
            submit.click()
            return cls.inspect(page)
        if before.health != PixelStormHealth.AUTH_REQUIRED:
            return before
        login_input, password_input, submit = (
            page.locator("[data-pixelstorm-login]"),
            page.locator("[data-pixelstorm-password]"),
            page.locator("[data-pixelstorm-submit]"),
        )
        if login_input.count() != 1 or password_input.count() != 1 or submit.count() != 1:
            return PixelStormPageInspection(PixelStormHealth.UNKNOWN_UI, PixelStormAuthResult.UNKNOWN_UI)
        login_input.fill(login)
        password_input.fill(password)
        submit.click()
        return cls.inspect(page)


class PlaywrightPixelStormAdapter:
    """Adapter over real Playwright ``Page`` objects, never a synthetic snapshot."""

    browser_safety = PixelStormBrowserSafety()

    def __init__(self, sessions: WebSessionStore, pages: dict[str, PageBoundary]) -> None:
        self._sessions = sessions
        self._pages = pages
        self._verification_factory: PixelStormCredentialVerificationFactory | None = None
        self._browser_sessions: PixelStormBrowserSessionFactory | None = None

    def with_browser_sessions(
        self, factory: PixelStormBrowserSessionFactory
    ) -> "PlaywrightPixelStormAdapter":
        self._browser_sessions = factory
        self._verification_factory = factory
        return self

    def with_credential_verification_factory(
        self, factory: PixelStormCredentialVerificationFactory
    ) -> "PlaywrightPixelStormAdapter":
        self._verification_factory = factory
        return self

    def _inspection(self, account_id: str) -> PixelStormPageInspection:
        page = self._pages.get(account_id)
        if page is None:
            return PixelStormPageInspection(PixelStormHealth.UNAVAILABLE, PixelStormAuthResult.UNAVAILABLE)
        return PixelStormPageObjects.inspect(page)

    def health(self, account_id: str) -> PixelStormHealth:
        inspection = self._inspection(account_id)
        if inspection.health != PixelStormHealth.READY:
            self._sessions.clear_pixelstorm_session(account_id)
        return inspection.health

    def inspect_authentication_state(self, account_id: str) -> PixelStormAuthenticationState:
        inspection = self._inspection(account_id)
        return PixelStormAuthenticationState(inspection.health, inspection.health == PixelStormHealth.READY)

    def inspect_session_state(self, account_id: str) -> PixelStormSessionState:
        inspection = self._inspection(account_id)
        session = self._sessions.get_pixelstorm_session(account_id)
        return PixelStormSessionState(inspection.health, session is not None, inspection.health == PixelStormHealth.READY)

    def inspect_security_capabilities(self, account_id: str) -> PixelStormSecurityCapabilities:
        inspection = self._inspection(account_id)
        page = self._pages.get(account_id)
        if page is None or inspection.health != PixelStormHealth.READY:
            return PixelStormSecurityCapabilities(False, False, False, False, inspection.health == PixelStormHealth.PIXEL_PASS_REQUIRED, True)
        declared = page.locator("[data-pixelstorm-page]").get_attribute(
            "data-pixelstorm-revoke-supported"
        )
        supported = declared == "true"
        password = page.locator("[data-pixelstorm-password-submit]").count() == 1
        return PixelStormSecurityCapabilities(
            supported, supported, supported, password, False, declared is None and not password
        )

    def authenticate(self, account_id: str, login: str, password: str, *, otp: str | None = None) -> PixelStormAuthResult:
        page = self._pages.get(account_id)
        if page is None:
            return PixelStormAuthResult.UNAVAILABLE
        result = PixelStormPageObjects.submit_synthetic_login(page, login, password, otp).authentication
        if result == PixelStormAuthResult.SUCCESS and self._browser_sessions is not None:
            self._browser_sessions.persist_page_session(account_id, page)
        return result

    def verify_credentials(self, account_id: str, login: str, password: str) -> PixelStormCredentialResult:
        if self._verification_factory is None:
            # Existing authenticated pages prove a session only, never supplied credentials.
            return PixelStormCredentialResult.AMBIGUOUS
        page = self._verification_factory.open_verification_page(account_id)
        if page is None:
            return PixelStormCredentialResult.UNAVAILABLE
        result = PixelStormPageObjects.submit_synthetic_login(page, login, password).authentication
        mapping = {
            PixelStormAuthResult.SUCCESS: PixelStormCredentialResult.VALID,
            PixelStormAuthResult.BAD_CREDENTIALS: PixelStormCredentialResult.INVALID,
            PixelStormAuthResult.PIXEL_PASS_REQUIRED: PixelStormCredentialResult.PIXEL_PASS_REQUIRED,
            PixelStormAuthResult.CHALLENGE: PixelStormCredentialResult.CHALLENGE,
            PixelStormAuthResult.WRONG_REGION: PixelStormCredentialResult.WRONG_REGION,
            PixelStormAuthResult.UNAVAILABLE: PixelStormCredentialResult.UNAVAILABLE,
        }
        return mapping.get(result, PixelStormCredentialResult.UNKNOWN_UI)

    def revoke_sessions(self, account_id: str) -> PixelStormRevocationResult:
        page = self._pages.get(account_id)
        if page is None:
            return PixelStormRevocationResult.UNKNOWN_UI
        capabilities = self.inspect_security_capabilities(account_id)
        if capabilities.unknown:
            return PixelStormRevocationResult.UNKNOWN_UI
        if not capabilities.session_revocation_available:
            return PixelStormRevocationResult.UNSUPPORTED
        action = page.locator("[data-pixelstorm-revoke]")
        if action.count() != 1:
            return PixelStormRevocationResult.UNSUPPORTED
        action.click()
        return self.verify_revocation(account_id)

    def verify_revocation(self, account_id: str) -> PixelStormRevocationResult:
        page = self._pages.get(account_id)
        if page is None or self._inspection(account_id).health != PixelStormHealth.READY:
            return PixelStormRevocationResult.UNKNOWN_UI
        main = page.locator("[data-pixelstorm-page]")
        if main.get_attribute("data-pixelstorm-revoke-supported") != "true":
            return PixelStormRevocationResult.UNSUPPORTED
        return PixelStormRevocationResult.SUPPORTED_VERIFIED if main.get_attribute("data-pixelstorm-sessions") == "revoked" else PixelStormRevocationResult.PARTIAL

    def change_password(self, account_id: str, login: str, current_password: str, pending_password: str) -> PixelStormPasswordChangeResult:
        del login
        return self._submit_synthetic_password(account_id, current_password, pending_password)

    def request_password_change(self, account_id: str, login: str, current_password: str) -> PixelStormPasswordChangeResult:
        del login
        page = self._pages.get(account_id)
        if page is None:
            return PixelStormPasswordChangeResult.UNAVAILABLE
        current = page.locator("[data-pixelstorm-current-password]")
        request = page.locator("[data-pixelstorm-password-request]")
        if current.count() != 1 or request.count() != 1:
            return self._password_page_error(account_id)
        current.fill(current_password)
        request.click()
        main = page.locator("[data-pixelstorm-page]")
        return PixelStormPasswordChangeResult.CONFIRMATION_REQUIRED if main.get_attribute("data-pixelstorm-password-requested") == "true" else PixelStormPasswordChangeResult.INVALID

    def complete_password_change(self, account_id: str, reset_url: str, pending_password: str) -> PixelStormPasswordChangeResult:
        if not reset_url:
            return PixelStormPasswordChangeResult.INVALID
        return self._submit_synthetic_password(account_id, "synthetic-current", pending_password)

    def _password_page_error(self, account_id: str) -> PixelStormPasswordChangeResult:
        health = self._inspection(account_id).health
        if health == PixelStormHealth.PIXEL_PASS_REQUIRED:
            return PixelStormPasswordChangeResult.PIXEL_PASS_REQUIRED
        if health == PixelStormHealth.CHALLENGE:
            return PixelStormPasswordChangeResult.CHALLENGE
        if health == PixelStormHealth.WRONG_REGION:
            return PixelStormPasswordChangeResult.WRONG_REGION
        return PixelStormPasswordChangeResult.UNKNOWN_UI

    def _submit_synthetic_password(
        self, account_id: str, current_password: str, pending_password: str
    ) -> PixelStormPasswordChangeResult:
        page = self._pages.get(account_id)
        if page is None:
            return PixelStormPasswordChangeResult.UNAVAILABLE
        current, pending, submit = (
            page.locator("[data-pixelstorm-current-password]"),
            page.locator("[data-pixelstorm-pending-password]"),
            page.locator("[data-pixelstorm-password-submit]"),
        )
        if current.count() != 1 or pending.count() != 1 or submit.count() != 1:
            return self._password_page_error(account_id)
        current.fill(current_password)
        pending.fill(pending_password)
        submit.click()
        state = page.locator("[data-pixelstorm-page]").get_attribute("data-pixelstorm-password-state")
        return PixelStormPasswordChangeResult.VERIFIED if state == "changed" else PixelStormPasswordChangeResult.INVALID
