from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.persistence.models import Base


class Database:
    def __init__(self, url: str) -> None:
        self.engine: Engine = create_engine(
            url, connect_args={"check_same_thread": False, "timeout": 15}
        )
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._enable_foreign_keys)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return self._sessions()

    @staticmethod
    def _enable_foreign_keys(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
