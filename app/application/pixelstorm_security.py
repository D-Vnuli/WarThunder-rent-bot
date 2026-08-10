"""Crash-safe Pixel Storm security operations with bounded maintenance auth."""

from datetime import datetime

from app.application.lease_guard import Clock, SideEffectLeaseGuard
from app.application.password_rotator import PasswordRotator
from app.application.pixelstorm_otp import PixelStormMaintenanceOtpService
from app.application.pixelstorm_passwords import PixelStormPasswordGenerator
from app.domain.notifications import OwnerNotification
from app.domain.pixelstorm import (
    PixelStormAuthResult,
    PixelStormCredentialResult,
    PixelStormHealth,
    PixelStormPasswordChangeResult,
    PixelStormRevocationResult,
    SecurityOperationOutcome,
)
from app.domain.ports import OwnerNotifier, PixelStormSecurityPort, SecureStorePort
from app.persistence.repositories import Repository


class PixelStormSecurityService:
    def __init__(
        self,
        pixelstorm: PixelStormSecurityPort,
        secrets: SecureStorePort,
        owner_notifier: OwnerNotifier | None = None,
        repository: Repository | None = None,
        maintenance_otp: PixelStormMaintenanceOtpService | None = None,
        password_generator: PixelStormPasswordGenerator | None = None,
        password_rotator: PasswordRotator | None = None,
        clock: Clock | None = None,
        lease_heartbeat_interval_seconds: float = 5.0,
    ) -> None:
        self._pixelstorm = pixelstorm
        self._secrets = secrets
        self._owner_notifier = owner_notifier
        self._repository = repository
        self._maintenance_otp = maintenance_otp
        self._passwords = password_generator or PixelStormPasswordGenerator()
        self._password_rotator = password_rotator
        self._waiting_external = False
        self._clock = clock
        self._lease_heartbeat_interval_seconds = lease_heartbeat_interval_seconds

    def execute_revoke(
        self,
        account_id: str,
        operation_id: str,
        now: datetime,
        *,
        recovery: bool = False,
        normal_claim_token: str | None = None,
        recovery_claim_token: str | None = None,
    ) -> SecurityOperationOutcome:
        self._waiting_external = False
        return SecurityOperationOutcome.COMPLETED if self.revoke(
            account_id,
            operation_id,
            now,
            recovery=recovery,
            normal_claim_token=normal_claim_token,
            recovery_claim_token=recovery_claim_token,
        ) else (SecurityOperationOutcome.WAITING_EXTERNAL if self._waiting_external else SecurityOperationOutcome.FAILED_CLOSED)

    def execute_rotate(
        self,
        account_id: str,
        operation_id: str,
        now: datetime,
        *,
        recovery: bool = False,
        normal_claim_token: str | None = None,
        recovery_claim_token: str | None = None,
    ) -> SecurityOperationOutcome:
        self._waiting_external = False
        return SecurityOperationOutcome.COMPLETED if self.rotate(
            account_id,
            operation_id,
            now,
            recovery=recovery,
            normal_claim_token=normal_claim_token,
            recovery_claim_token=recovery_claim_token,
        ) else (SecurityOperationOutcome.WAITING_EXTERNAL if self._waiting_external else SecurityOperationOutcome.FAILED_CLOSED)

    def revoke(
        self,
        account_id: str,
        operation_id: str,
        now: datetime,
        *,
        recovery: bool = False,
        normal_claim_token: str | None = None,
        recovery_claim_token: str | None = None,
    ) -> bool:
        if not self._ensure_authenticated(
            account_id, operation_id, now, normal_claim_token, recovery_claim_token
        ):
            return False
        capabilities = self._pixelstorm.inspect_security_capabilities(account_id)
        if capabilities.unknown or not (capabilities.session_revocation_available or capabilities.revoke_all_available):
            self._notify("SESSION_REVOCATION_UNSUPPORTED", operation_id, account_id, now)
            return False
        if recovery:
            result = self._pixelstorm.verify_revocation(account_id)
            if result == PixelStormRevocationResult.SUPPORTED_VERIFIED:
                return True
            self._notify("SESSION_REVOCATION_AMBIGUOUS", operation_id, account_id, now)
            return False
        owned, result = self._external_side_effect(
            operation_id,
            now,
            normal_claim_token,
            recovery_claim_token,
            lambda: self._pixelstorm.revoke_sessions(account_id),
        )
        if not owned or result is None:
            return False
        if result == PixelStormRevocationResult.SUPPORTED_VERIFIED and self._pixelstorm.verify_revocation(account_id) == PixelStormRevocationResult.SUPPORTED_VERIFIED:
            return True
        self._notify("SESSION_REVOCATION_AMBIGUOUS", operation_id, account_id, now)
        return False

    def rotate(
        self,
        account_id: str,
        operation_id: str,
        now: datetime,
        *,
        recovery: bool = False,
        normal_claim_token: str | None = None,
        recovery_claim_token: str | None = None,
    ) -> bool:
        if not self._ensure_authenticated(
            account_id, operation_id, now, normal_claim_token, recovery_claim_token
        ):
            return False
        capabilities = self._pixelstorm.inspect_security_capabilities(account_id)
        if capabilities.unknown or not capabilities.password_change_available:
            self._notify("PASSWORD_ROTATION_AMBIGUOUS", operation_id, account_id, now)
            return False
        current = self._secrets.get_current_credentials(account_id)
        if current is None:
            self._notify("PASSWORD_VERIFICATION_FAILED", operation_id, account_id, now)
            return False
        pending = self._secrets.get_pending_credentials(account_id)
        operation = self._repository.get_operation(operation_id) if self._repository is not None else None
        if operation is not None and operation.security_state == "WAITING_PASSWORD_CHANGE_EMAIL":
            if pending is None or self._password_rotator is None:
                self._notify("PASSWORD_ROTATION_AMBIGUOUS", operation_id, account_id, now)
                return False
            reset_url = self._password_rotator.consume_expected_reset_url(operation_id, now)
            if reset_url is None:
                self._wait(operation_id, "WAITING_PASSWORD_CHANGE_EMAIL", now)
                return False
            owned, changed = self._external_side_effect(
                operation_id,
                now,
                normal_claim_token,
                recovery_claim_token,
                lambda: self._pixelstorm.complete_password_change(account_id, reset_url, pending[1]),
            )
            if not owned or changed is None:
                return False
            return self._finalize_password_change(
                changed,
                account_id,
                operation_id,
                now,
                pending,
            )
        if pending is None:
            state = operation.security_state if operation is not None else "INIT"
            if recovery and state in {"REMOTE_VERIFIED", "SECRET_PROMOTED"}:
                if self._pixelstorm.verify_credentials(account_id, *current) == PixelStormCredentialResult.VALID:
                    return True
                self._notify("PASSWORD_ROTATION_AMBIGUOUS", operation_id, account_id, now)
                return False
            pending = (current[0], self._passwords.generate())
            self._secrets.set_pending_credentials(account_id, *pending)
            self._set_state(operation_id, "PENDING_READY", now)
        if self._pixelstorm.verify_credentials(account_id, *pending) == PixelStormCredentialResult.VALID:
            self._set_state(operation_id, "REMOTE_VERIFIED", now)
            self._secrets.promote_pending_credentials(account_id)
            self._set_state(operation_id, "SECRET_PROMOTED", now)
            return True
        if self._pixelstorm.verify_credentials(account_id, *current) != PixelStormCredentialResult.VALID:
            self._notify("PASSWORD_VERIFICATION_FAILED", operation_id, account_id, now)
            return False
        # A persisted intent without a confirmed email is deliberately not
        # retried: after a crash, a duplicate request is less safe than review.
        if operation is not None and operation.password_change_requested_at is not None:
            self._wait(operation_id, "WAITING_PASSWORD_CHANGE_EMAIL", now)
            return False
        if self._repository is not None and not self._repository.record_password_change_requested(
            operation_id, now
        ):
            self._wait(operation_id, "WAITING_PASSWORD_CHANGE_EMAIL", now)
            return False
        owned, requested = self._external_side_effect(
            operation_id,
            now,
            normal_claim_token,
            recovery_claim_token,
            lambda: self._pixelstorm.request_password_change(account_id, current[0], current[1]),
        )
        if not owned or requested is None:
            return False
        if requested == PixelStormPasswordChangeResult.CONFIRMATION_REQUIRED:
            if self._password_rotator is None:
                self._notify("PASSWORD_ROTATION_AMBIGUOUS", operation_id, account_id, now)
                return False
            reset_url = self._password_rotator.consume_expected_reset_url(operation_id, now)
            if reset_url is None:
                self._wait(operation_id, "WAITING_PASSWORD_CHANGE_EMAIL", now)
                return False
            owned, changed = self._external_side_effect(
                operation_id,
                now,
                normal_claim_token,
                recovery_claim_token,
                lambda: self._pixelstorm.complete_password_change(account_id, reset_url, pending[1]),
            )
            if not owned or changed is None:
                return False
        elif requested == PixelStormPasswordChangeResult.VERIFIED:
            owned, changed = self._external_side_effect(
                operation_id,
                now,
                normal_claim_token,
                recovery_claim_token,
                lambda: self._pixelstorm.change_password(account_id, current[0], current[1], pending[1]),
            )
            if not owned or changed is None:
                return False
        else:
            changed = requested
        return self._finalize_password_change(changed, account_id, operation_id, now, pending)

    def _finalize_password_change(
        self,
        changed: PixelStormPasswordChangeResult,
        account_id: str,
        operation_id: str,
        now: datetime,
        pending: tuple[str, str],
    ) -> bool:
        if changed != PixelStormPasswordChangeResult.VERIFIED or self._pixelstorm.verify_credentials(account_id, *pending) != PixelStormCredentialResult.VALID:
            self._notify(self._password_category(changed), operation_id, account_id, now)
            return False
        self._set_state(operation_id, "REMOTE_VERIFIED", now)
        self._secrets.promote_pending_credentials(account_id)
        self._set_state(operation_id, "SECRET_PROMOTED", now)
        return True

    def _ensure_authenticated(
        self,
        account_id: str,
        operation_id: str,
        now: datetime,
        normal_claim_token: str | None,
        recovery_claim_token: str | None,
    ) -> bool:
        health = self._pixelstorm.health(account_id)
        if health == PixelStormHealth.READY:
            return True
        if health != PixelStormHealth.AUTH_REQUIRED:
            self._notify(self._health_category(health), operation_id, account_id, now)
            return False
        credentials = self._secrets.get_current_credentials(account_id)
        if credentials is None:
            self._notify("PIXEL_STORM_AUTH_REQUIRED", operation_id, account_id, now)
            return False
        operation = self._repository.get_operation(operation_id) if self._repository is not None else None
        waiting_for_otp = operation is not None and operation.security_state == "WAITING_LOGIN_OTP"
        if waiting_for_otp and self._maintenance_otp is not None:
            assert operation is not None
            otp = self._maintenance_otp.consume(
                account_id, operation_id, operation.maintenance_login_requested_at or now, now
            )
            if otp is None:
                self._waiting_external = True
                self._wait(operation_id, "WAITING_LOGIN_OTP", now)
                return False
            return self._authenticate_once_with_otp(
                account_id, operation_id, now, credentials, otp, normal_claim_token, recovery_claim_token
            )
        if self._repository is not None:
            self._repository.record_maintenance_login_requested(operation_id, now)
        owned, result = self._external_side_effect(
            operation_id,
            now,
            normal_claim_token,
            recovery_claim_token,
            lambda: self._pixelstorm.authenticate(account_id, *credentials),
        )
        if not owned or result is None:
            return False
        if result == PixelStormAuthResult.SUCCESS:
            return True
        if result != PixelStormAuthResult.EMAIL_OTP_REQUIRED or self._maintenance_otp is None:
            self._notify(self._auth_category(result), operation_id, account_id, now)
            return False
        otp = self._maintenance_otp.consume(account_id, operation_id, now, now)
        if otp is None:
            self._wait(operation_id, "WAITING_LOGIN_OTP", now)
            return False
        return self._authenticate_once_with_otp(
            account_id, operation_id, now, credentials, otp, normal_claim_token, recovery_claim_token
        )

    def _authenticate_once_with_otp(
        self,
        account_id: str,
        operation_id: str,
        now: datetime,
        credentials: tuple[str, str],
        otp: str,
        normal_claim_token: str | None,
        recovery_claim_token: str | None,
    ) -> bool:
        owned, result = self._external_side_effect(
            operation_id,
            now,
            normal_claim_token,
            recovery_claim_token,
            lambda: self._pixelstorm.authenticate(account_id, *credentials, otp=otp),
        )
        if not owned or result is None:
            return False
        if result == PixelStormAuthResult.SUCCESS:
            return True
        self._notify(self._auth_category(result), operation_id, account_id, now)
        return False

    def _fence(
        self,
        operation_id: str,
        now: datetime,
        normal_claim_token: str | None,
        recovery_claim_token: str | None,
    ) -> bool:
        if self._repository is None:
            return True
        fence_now = self._clock.now() if self._clock is not None else now
        if recovery_claim_token is not None:
            return self._repository.fence_recovery_side_effect(
                operation_id, recovery_claim_token, fence_now
            )
        if normal_claim_token is None:
            normal_claim_token = self._repository.get_operation(operation_id).normal_claim_token
        return self._repository.fence_normal_side_effect(operation_id, normal_claim_token, fence_now)

    def _external_side_effect(
        self,
        operation_id: str,
        now: datetime,
        normal_claim_token: str | None,
        recovery_claim_token: str | None,
        action,
    ):
        if self._repository is None:
            return True, action()
        if normal_claim_token is None and recovery_claim_token is None:
            normal_claim_token = self._repository.get_operation(operation_id).normal_claim_token
        return SideEffectLeaseGuard(
            self._repository,
            operation_id,
            normal_claim_token=normal_claim_token,
            recovery_claim_token=recovery_claim_token,
            clock=self._clock,
            fallback_now=now,
            heartbeat_interval_seconds=self._lease_heartbeat_interval_seconds,
        ).run(action)

    @staticmethod
    def _health_category(health: PixelStormHealth) -> str:
        return {
            PixelStormHealth.AUTH_REQUIRED: "PIXEL_STORM_AUTH_REQUIRED",
            PixelStormHealth.PIXEL_PASS_REQUIRED: "PIXEL_PASS_REQUIRED",
            PixelStormHealth.WRONG_REGION: "PIXEL_STORM_WRONG_REGION",
            PixelStormHealth.CHALLENGE: "PIXEL_STORM_CHALLENGE",
        }.get(health, "PIXEL_STORM_UNKNOWN_UI")

    @classmethod
    def _auth_category(cls, result: PixelStormAuthResult) -> str:
        if result == PixelStormAuthResult.BAD_CREDENTIALS:
            return "PIXEL_STORM_AUTH_REQUIRED"
        return cls._health_category(PixelStormHealth(result) if result.value in PixelStormHealth._value2member_map_ else PixelStormHealth.UNKNOWN_UI)

    @classmethod
    def _password_category(cls, result: PixelStormPasswordChangeResult) -> str:
        if result == PixelStormPasswordChangeResult.INVALID:
            return "PASSWORD_VERIFICATION_FAILED"
        return cls._health_category(PixelStormHealth(result) if result.value in PixelStormHealth._value2member_map_ else PixelStormHealth.UNKNOWN_UI)

    def _notify(self, category: str, operation_id: str, account_id: str, now: datetime) -> None:
        if self._owner_notifier is not None:
            self._owner_notifier.notify(OwnerNotification(category, operation_id, now, account_id=account_id))

    def _wait(self, operation_id: str, state: str, now: datetime) -> None:
        self._waiting_external = True
        if self._repository is not None:
            self._repository.set_security_state(operation_id, state, now)

    def _set_state(self, operation_id: str, state: str, now: datetime) -> None:
        if self._repository is not None:
            self._repository.set_security_state(operation_id, state, now)
