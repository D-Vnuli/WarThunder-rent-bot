"""Safe operator CLI; commands never initialize live external transports."""

import argparse
import json
from pathlib import Path
from typing import Any

from alembic import command
from app.config.settings import RuntimeMode, Settings
from app.persistence.database import Database
from app.production import (
    CheckState,
    _alembic_config,
    create_diagnostics,
    database_check,
    preflight,
    safe_status,
    sqlite_backup,
    validate_backup,
)


def _settings(args: argparse.Namespace) -> Settings:
    values: dict[str, Any] = {"app_mode": RuntimeMode.PRODUCTION_DRY_RUN, "dry_run": True}
    if args.database_url:
        values["database_url"] = args.database_url
    if args.runtime_dir:
        values["runtime_dir"] = Path(args.runtime_dir)
        values["backup_dir"] = Path(args.runtime_dir) / "backups"
        values["log_path"] = Path(args.runtime_dir) / "logs" / "app.jsonl"
        values["secure_store_path"] = Path(args.runtime_dir) / "secure-store.vault"
        values["web_session_store_path"] = Path(args.runtime_dir) / "web-session.vault"
    return Settings(**values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="War Thunder rent-bot safe operator CLI")
    parser.add_argument("--database-url")
    parser.add_argument("--runtime-dir")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    commands.add_parser("preflight")
    commands.add_parser("status")
    commands.add_parser("db-check")
    commands.add_parser("backup")
    check = commands.add_parser("backup-check")
    check.add_argument("file")
    diagnostics = commands.add_parser("diagnostics")
    diagnostics.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    settings = _settings(args)
    database = Database(settings.database_url)

    if args.command == "migrate":
        command.upgrade(_alembic_config(settings.database_url), "head")
        print("MIGRATED")
        return 0
    if args.command == "db-check":
        result = database_check(database)
        print(result.reason)
        return 0 if result.state == CheckState.READY else 2
    checks = preflight(settings, database)
    if args.command == "preflight":
        print(json.dumps([item.__dict__ for item in checks]))
        return 0 if all(item.state != CheckState.NOT_READY for item in checks) else 2
    if args.command == "status":
        print(json.dumps(safe_status(settings, database, checks), default=str))
        return 0
    if args.command == "backup":
        print(sqlite_backup(settings.database_url, settings.backup_dir, settings.backup_retention))
        return 0
    if args.command == "backup-check":
        result = validate_backup(Path(args.file), settings.database_url)
        print(result.reason)
        return 0 if result.state == CheckState.READY else 2
    if args.command == "diagnostics":
        print(create_diagnostics(settings, database, checks, Path(args.output)))
        return 0
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
