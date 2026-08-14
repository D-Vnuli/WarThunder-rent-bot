from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.persistence.models import Base


class Database:
    def __init__(self, url: str) -> None:
        self._active_transactions = 0
        self.engine: Engine = create_engine(
            url, connect_args={"check_same_thread": False, "timeout": 15}
        )
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._enable_foreign_keys)
            # Establish WAL once when practical.  It must not run as a connect
            # hook because simultaneous worker startup can contend on SQLite's
            # journal-mode transition before normal lease recovery begins.
            try:
                with self.engine.connect() as connection:
                    connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            except Exception:
                # A concurrent opener will observe the already-selected mode;
                # normal DB/integrity preflight remains the authority.
                pass
        event.listen(self.engine, "begin", self._transaction_started)
        event.listen(self.engine, "commit", self._transaction_finished)
        event.listen(self.engine, "rollback", self._transaction_finished)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return self._sessions()

    @property
    def active_transactions(self) -> int:
        """Diagnostic-only counter for asserting side-effect transaction boundaries."""
        return self._active_transactions

    def _transaction_started(self, _: object) -> None:
        self._active_transactions += 1

    def _transaction_finished(self, _: object) -> None:
        self._active_transactions = max(0, self._active_transactions - 1)

    @staticmethod
    def _enable_foreign_keys(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()
