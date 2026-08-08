from datetime import datetime
from secrets import token_urlsafe

from app.domain.models import OrderInput, StartResult
from app.domain.ports import FunPayPort, GaijinPort, SecureStorePort
from app.domain.states import OperationKind
from app.persistence.repositories import Repository, StateConflictError


class RentalManager:
    def __init__(
        self,
        repository: Repository,
        funpay: FunPayPort,
        gaijin: GaijinPort,
        secrets: SecureStorePort,
    ) -> None:
        self.repository = repository
        self.funpay = funpay
        self.gaijin = gaijin
        self.secrets = secrets

    def accept_order(self, order: OrderInput, now: datetime) -> StartResult:
        return self.repository.reserve_order(order, now)

    def extend_rental(
        self, rental_id: str, buyer_id: str, extension_seconds: int, now: datetime
    ) -> bool:
        return self.repository.extend_active_rental(rental_id, buyer_id, extension_seconds, now)

    def run_operations(self, now: datetime) -> None:
        for operation in self.repository.pending_operations():
            claimed = self.repository.claim_operation(operation.id, now)
            if claimed is None:
                continue
            try:
                prepared = self.repository.prepare_operation(claimed.id, now)
            except (StateConflictError, ValueError):
                self.repository.operation_failed(claimed.id, now)
                continue
            if prepared is None:
                continue
            operation = prepared
            completed = False
            if operation.kind == OperationKind.DISABLE_LOTS:
                completed = self.funpay.disable_lots(operation.account_id).verified
            elif operation.kind == OperationKind.SEND_CREDENTIALS:
                completed = self.funpay.send_credentials(
                    operation.rental_id or "", idempotency_key=operation.idempotency_key
                )
            elif operation.kind == OperationKind.REVOKE_SESSIONS:
                completed = self.gaijin.revoke_sessions(operation.account_id)
            elif operation.kind == OperationKind.ROTATE_PASSWORD:
                self.secrets.set_pending(operation.account_id, token_urlsafe(24))
                completed = self.gaijin.rotate_password(
                    operation.account_id
                ) and self.gaijin.verify_access(operation.account_id)
                if completed:
                    self.secrets.promote_pending(operation.account_id)
            elif operation.kind == OperationKind.ENABLE_LOTS:
                completed = self.funpay.enable_lots(operation.account_id).verified
            if completed:
                self.repository.operation_completed(operation.id, now)
            else:
                self.repository.operation_failed(operation.id, now)
