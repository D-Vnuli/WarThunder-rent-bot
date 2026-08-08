from datetime import datetime

from app.application.rental_manager import RentalManager
from app.persistence.repositories import Repository


class DurableScheduler:
    def __init__(self, repository: Repository, manager: RentalManager) -> None:
        self.repository = repository
        self.manager = manager

    def tick(self, now: datetime) -> None:
        self.repository.expire_due(now)
        self.manager.run_operations(now)
