from datetime import datetime

from app.domain.funpay import FunPayHealth
from app.domain.models import OrderInput, StartResult
from app.domain.notifications import OwnerNotification
from app.domain.pixelstorm import SecurityOperationOutcome
from app.domain.ports import FunPayPort, GaijinPort, OwnerNotifier, SecureStorePort
from app.domain.states import OperationKind, OperationStatus
from app.persistence.repositories import Repository, StateConflictError
from app.templates.funpay_messages import credential_message


class RentalManager:
    def __init__(
        self,
        repository: Repository,
        funpay: FunPayPort,
        gaijin: GaijinPort | None,
        secrets: SecureStorePort,
        owner_notifier: OwnerNotifier | None = None,
        otp_service=None,
        message_receipts=None,
        pixelstorm_security=None,
    ) -> None:
        self.repository = repository
        self.funpay = funpay
        self.gaijin = gaijin
        self.secrets = secrets
        self._owner_notifier = owner_notifier
        self._otp_service = otp_service
        self._message_receipts = message_receipts
        self._pixelstorm_security = pixelstorm_security

    def accept_order(self, order: OrderInput, now: datetime) -> StartResult:
        result = self.repository.reserve_order(order, now)
        if not result.accepted:
            self._notify("PAID_ORDER_BLOCKED", order.funpay_order_id, now, order_id=order.funpay_order_id)
        return result

    def extend_rental(
        self, rental_id: str, buyer_id: str, extension_seconds: int, now: datetime
    ) -> bool:
        return self.repository.extend_active_rental(rental_id, buyer_id, extension_seconds, now)

    def run_operations(self, now: datetime) -> None:
        operations = self.repository.pending_operations() + self.repository.waiting_security_operations()
        for operation in operations:
            if operation.kind in {
                OperationKind.DISABLE_LOTS,
                OperationKind.SEND_CREDENTIALS,
                OperationKind.ENABLE_LOTS,
                OperationKind.SEND_OTP,
            } and self.funpay.health() != FunPayHealth.READY:
                self._notify_health(self.funpay.health(), operation.account_id, now)
                continue
            if operation.status == OperationStatus.PENDING:
                claimed = self.repository.claim_operation(operation.id, now)
                if claimed is None:
                    continue
            else:
                claimed = operation
            try:
                prepared = self.repository.prepare_operation(claimed.id, now)
            except (StateConflictError, ValueError):
                self.repository.operation_failed(claimed.id, now)
                continue
            if prepared is None:
                continue
            operation = prepared
            completed = False
            outcome: SecurityOperationOutcome | None = None
            if operation.kind == OperationKind.DISABLE_LOTS:
                lot_ids = self.repository.account_lot_ids(operation.account_id)
                if not lot_ids:
                    self.repository.operation_failed(operation.id, now)
                    continue
                completed = self.funpay.disable_lots(operation.account_id, lot_ids).verified
            elif operation.kind == OperationKind.SEND_CREDENTIALS:
                completed = self._send_credentials(operation, now)
            elif operation.kind == OperationKind.REVOKE_SESSIONS:
                outcome = self._pixelstorm_security.execute_revoke(operation.account_id, operation.id, now) if self._pixelstorm_security is not None else SecurityOperationOutcome.FAILED_CLOSED
                completed = outcome == SecurityOperationOutcome.COMPLETED
            elif operation.kind == OperationKind.ROTATE_PASSWORD:
                outcome = self._pixelstorm_security.execute_rotate(operation.account_id, operation.id, now) if self._pixelstorm_security is not None else SecurityOperationOutcome.FAILED_CLOSED
                completed = outcome == SecurityOperationOutcome.COMPLETED
            elif operation.kind == OperationKind.ENABLE_LOTS:
                lot_ids = self.repository.account_lot_ids(operation.account_id)
                if not lot_ids:
                    self.repository.operation_failed(operation.id, now)
                    continue
                completed = self.funpay.enable_lots(operation.account_id, lot_ids).verified
            elif operation.kind == OperationKind.SEND_OTP:
                completed = self._send_otp(operation, now)
            if completed:
                self.repository.operation_completed(operation.id, now)
            elif outcome == SecurityOperationOutcome.WAITING_EXTERNAL:
                continue
            else:
                self.repository.operation_failed(operation.id, now)
                if operation.kind in {OperationKind.DISABLE_LOTS, OperationKind.ENABLE_LOTS}:
                    self._notify(
                        f"{operation.kind}_VERIFICATION_FAILED",
                        operation.idempotency_key,
                        now,
                        account_id=operation.account_id,
                        rental_id=operation.rental_id,
                    )
                elif operation.kind in {OperationKind.SEND_CREDENTIALS, OperationKind.SEND_OTP}:
                    self._notify(
                        f"{operation.kind}_FAIL_CLOSED",
                        operation.idempotency_key,
                        now,
                        account_id=operation.account_id,
                        rental_id=operation.rental_id,
                    )

    def _notify_health(self, health, account_id: str, now: datetime) -> None:
        if health != FunPayHealth.READY:
            self._notify(f"FUNPAY_{health}", f"FUNPAY:{health}:{account_id}", now, account_id=account_id)

    def notify_operation_failure(self, operation, category: str, now: datetime) -> None:
        self._notify(
            category,
            operation.idempotency_key,
            now,
            account_id=operation.account_id,
            rental_id=operation.rental_id,
        )

    def _notify(
        self,
        category: str,
        correlation_id: str,
        now: datetime,
        *,
        account_id: str | None = None,
        rental_id: str | None = None,
        order_id: str | None = None,
        event_id: str | None = None,
        safe_error_category: str | None = None,
    ) -> None:
        if self._owner_notifier is not None:
            self._owner_notifier.notify(
                OwnerNotification(category, correlation_id, now, account_id, rental_id, order_id, event_id, safe_error_category)
            )

    def recover_message_receipts(self, now: datetime) -> int:
        recovered = 0
        for operation in self.repository.recoverable_message_operations():
            receipt = self.funpay.get_message_receipt(operation.idempotency_key)
            if receipt is None:
                continue
            if receipt.ambiguous or not (receipt.delivered and receipt.verified):
                self.repository.operation_failed(operation.id, now)
                self._notify(
                    f"{operation.kind}_AMBIGUOUS" if receipt.ambiguous else f"{operation.kind}_FAIL_CLOSED",
                    operation.idempotency_key,
                    now,
                    account_id=operation.account_id,
                    rental_id=operation.rental_id,
                    safe_error_category=receipt.safe_error_category,
                )
            else:
                self.repository.operation_completed(operation.id, now)
                recovered += 1
        return recovered

    def _send_credentials(self, operation, now: datetime) -> bool:
        receipt = self.funpay.get_message_receipt(operation.idempotency_key)
        if receipt is not None:
            if receipt.ambiguous:
                return False
            if receipt.delivered and receipt.verified:
                return True
        credentials = self.secrets.get_current_credentials(operation.account_id)
        if credentials is None:
            return False
        rental = self.repository.get_rental(operation.rental_id or "")
        receipt = self.funpay.send_message(
            rental.buyer_id,
            credential_message(credentials[0], credentials[1], rental.expires_at),
            idempotency_key=operation.idempotency_key,
            now=now,
        )
        if self._message_receipts is not None:
            self._message_receipts.record_receipt(receipt, None)
        return receipt.delivered and receipt.verified and not receipt.ambiguous

    def _send_otp(self, operation, now: datetime) -> bool:
        receipt = self.funpay.get_message_receipt(operation.idempotency_key)
        if receipt is not None:
            return receipt.delivered and receipt.verified and not receipt.ambiguous
        if self._otp_service is None or operation.rental_id is None:
            return False
        rental = self.repository.get_rental(operation.rental_id)
        otp = self._otp_service.request_otp(rental.id, rental.buyer_id, operation.created_at, now)
        if otp is None:
            return False
        receipt = self.funpay.send_message(
            rental.buyer_id, otp, idempotency_key=operation.idempotency_key, now=now
        )
        if self._message_receipts is not None:
            self._message_receipts.record_receipt(receipt, None)
        return receipt.delivered and receipt.verified and not receipt.ambiguous
