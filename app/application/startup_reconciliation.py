from datetime import datetime

from app.persistence.repositories import Repository


class StartupReconciliation:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def run(self, now: datetime) -> int:
        return self.repository.recover_expired_leases(now) + self.repository.reconcile(now)
