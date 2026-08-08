from datetime import datetime

from app.application.rental_manager import RentalManager
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
        return recovered + self.repository.recover_expired_leases(now) + self.repository.reconcile(now)
