"""Fresh-clock durable lease guards for bounded external side effects."""

from collections.abc import Callable
from datetime import UTC, datetime
from threading import Event, Thread
from typing import Protocol, TypeVar

from app.persistence.repositories import Repository


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


Result = TypeVar("Result")


class SideEffectLeaseGuard:
    """Keep one durable claim alive while an external call remains in flight."""

    def __init__(
        self,
        repository: Repository,
        operation_id: str,
        *,
        normal_claim_token: str | None = None,
        recovery_claim_token: str | None = None,
        clock: Clock | None = None,
        fallback_now: datetime,
        heartbeat_interval_seconds: float = 5.0,
    ) -> None:
        self._repository = repository
        self._operation_id = operation_id
        self._normal_claim_token = normal_claim_token
        self._recovery_claim_token = recovery_claim_token
        self._clock = clock
        self._fallback_now = fallback_now
        self._interval = heartbeat_interval_seconds
        self._stop = Event()
        self._lost = Event()
        self._thread: Thread | None = None
        self._unsubscribe: Callable[[], None] | None = None

    def run(self, action: Callable[[], Result]) -> tuple[bool, Result | None]:
        if not self._renew():
            return False, None
        subscribe = getattr(self._clock, "subscribe", None)
        if callable(subscribe):
            self._unsubscribe = subscribe(self._on_clock_advance)
        self._thread = Thread(target=self._heartbeat, daemon=True)
        self._thread.start()
        try:
            result = action()
        finally:
            self._stop.set()
            self._thread.join()
            if self._unsubscribe is not None:
                self._unsubscribe()
        return not self._lost.is_set(), result

    def _fresh_now(self) -> datetime:
        return self._clock.now() if self._clock is not None else self._fallback_now

    def _renew(self) -> bool:
        now = self._fresh_now()
        if self._recovery_claim_token is not None:
            return self._repository.fence_recovery_side_effect(
                self._operation_id, self._recovery_claim_token, now
            )
        return self._repository.fence_normal_side_effect(
            self._operation_id, self._normal_claim_token, now
        )

    def _heartbeat(self) -> None:
        while not self._stop.wait(self._interval):
            if not self._renew():
                self._lost.set()
                return

    def _on_clock_advance(self) -> None:
        if not self._stop.is_set() and not self._lost.is_set() and not self._renew():
            self._lost.set()
