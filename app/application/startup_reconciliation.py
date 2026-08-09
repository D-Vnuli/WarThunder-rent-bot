from datetime import datetime

from app.application.rental_manager import RentalManager
from app.domain.pixelstorm import SecurityOperationOutcome
from app.domain.ports import FunPayPort
from app.domain.states import OperationKind
from app.persistence.repositories import Repository


class StartupReconciliation:
    def __init__(
        self,
        repository: Repository,
        manager: RentalManager | None = None,
        funpay: FunPayPort | None = None,
    ) -> None:
        self.repository = repository
        self._manager = manager
        self._funpay = funpay

    def run(self, now: datetime) -> int:
        recovered = self._manager.recover_message_receipts(now) if self._manager is not None else 0
        if self._funpay is not None:
            for operation in self.repository.running_operations():
                if operation.kind not in {OperationKind.DISABLE_LOTS, OperationKind.ENABLE_LOTS}:
                    continue
                lot_ids = self.repository.account_lot_ids(operation.account_id)
                verified = bool(lot_ids) and (
                    self._funpay.verify_lots_disabled(lot_ids).verified
                    if operation.kind == OperationKind.DISABLE_LOTS
                    else self._funpay.verify_lots_enabled(lot_ids).verified
                )
                if verified:
                    self.repository.operation_completed(operation.id, now)
                else:
                    self.repository.operation_failed(operation.id, now)
                    if self._manager is not None:
                        self._manager.notify_operation_failure(
                            operation, f"{operation.kind}_VERIFICATION_FAILED", now
                        )
                recovered += 1
        if self._manager is not None and getattr(self._manager, "_pixelstorm_security", None) is not None:
            for operation in self.repository.running_operations():
                if operation.security_state in {
                    "WAITING_LOGIN_OTP",
                    "WAITING_PASSWORD_CHANGE_EMAIL",
                }:
                    # Waiting for a durably-correlated email is valid work.  Do
                    # not claim it: the regular worker will resume it later.
                    continue
                recovery_operation = self.repository.claim_startup_recovery(operation.id, now)
                if recovery_operation is None:
                    continue
                operation = recovery_operation
                token = operation.recovery_claim_token
                if token is None:
                    self.repository.operation_failed(operation.id, now)
                    continue
                if operation.kind == OperationKind.REVOKE_SESSIONS:
                    # The prior worker may have died after claiming but before
                    # persisting the pre-side-effect REVOKING state.
                    operation = self.repository.prepare_operation(operation.id, now) or operation
                    outcome = self._manager._pixelstorm_security.execute_revoke(
                        operation.account_id, operation.id, now, recovery=True
                    )
                elif operation.kind == OperationKind.ROTATE_PASSWORD:
                    outcome = self._manager._pixelstorm_security.execute_rotate(
                        operation.account_id, operation.id, now, recovery=True
                    )
                else:
                    continue
                if outcome == SecurityOperationOutcome.COMPLETED:
                    self.repository.complete_recovery_operation(operation.id, token, now)
                elif outcome == SecurityOperationOutcome.FAILED_CLOSED:
                    self.repository.fail_recovery_operation(operation.id, token, now)
                else:
                    self.repository.release_recovery_claim(operation.id, token, now)
                recovered += 1
        return recovered + self.repository.recover_expired_leases(now) + self.repository.reconcile(now)
