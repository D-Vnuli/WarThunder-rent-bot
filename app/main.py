from app.application.startup_reconciliation import StartupReconciliation
from app.config.settings import Settings
from app.persistence.database import Database
from app.persistence.repositories import Repository


def create_application() -> Database:
    settings = Settings()
    settings.require_safe_mode()
    database = Database(settings.database_url)
    # Production schema is owned exclusively by Alembic migrations.
    StartupReconciliation(Repository(database)).run(
        __import__("datetime").datetime.now(__import__("datetime").UTC)
    )
    return database
