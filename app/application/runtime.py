"""Production-usable single-cycle orchestration boundary."""

from dataclasses import dataclass
from datetime import datetime

from app.application.email_event_dispatcher import EmailEventDispatcher
from app.application.funpay_dispatcher import FunPayEventDispatcher, FunPayEventPoller
from app.application.gmail_watcher import GmailWatcher
from app.application.rental_manager import RentalManager
from app.application.scheduler import DurableScheduler
from app.application.startup_reconciliation import StartupReconciliation
from app.persistence.repositories import Repository


@dataclass
class ApplicationRuntime:
    repository: Repository
    manager: RentalManager
    funpay_poller: FunPayEventPoller
    funpay_dispatcher: FunPayEventDispatcher
    gmail_watcher: GmailWatcher
    email_dispatcher: EmailEventDispatcher
    scheduler: DurableScheduler
    startup: StartupReconciliation
    last_poll_at: datetime
    resource_closers: tuple[object, ...] = ()

    def run_once(self, now: datetime) -> None:
        """One bounded, deterministic processing cycle; no background loop."""
        self.repository.recover_expired_leases(now)
        self.funpay_poller.poll_once(self.last_poll_at, now)
        self.funpay_dispatcher.dispatch_pending(now)
        self.gmail_watcher.poll_once(self.last_poll_at, now)
        self.email_dispatcher.dispatch_pending(now)
        self.scheduler.tick(now)
        # New durable work created by the scheduler may be safely advanced on
        # the following bounded pass without holding a DB transaction open.
        self.manager.run_operations(now)
        self.last_poll_at = now

    def reconcile_startup(self, now: datetime) -> int:
        return self.startup.run(now)

    def close(self) -> None:
        for resource in self.resource_closers:
            close = getattr(resource, "close", None)
            if callable(close):
                close()
        engine = getattr(getattr(self.repository, "db", None), "engine", None)
        if engine is not None:
            engine.dispose()
