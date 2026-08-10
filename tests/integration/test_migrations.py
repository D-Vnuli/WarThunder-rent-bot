import os
import sqlite3
import subprocess
import sys


def test_upgrade_downgrade_upgrade_uses_immutable_revision(tmp_path):
    database = tmp_path / "migration.db"
    environment = {**os.environ, "DATABASE_URL": f"sqlite:///{database.as_posix()}"}
    command = [sys.executable, "-m", "alembic", "-c", "alembic.ini"]
    subprocess.run([*command, "upgrade", "head"], check=True, env=environment)


def test_phase2_upgrade_and_downgrade_preserve_phase1_schema(tmp_path):
    database = tmp_path / "phase2-migration.db"
    environment = {**os.environ, "DATABASE_URL": f"sqlite:///{database.as_posix()}"}
    command = [sys.executable, "-m", "alembic", "-c", "alembic.ini"]
    subprocess.run([*command, "upgrade", "0001_phase1_core"], check=True, env=environment)
    subprocess.run([*command, "upgrade", "0002_phase2_email_security"], check=True, env=environment)
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='table'")
        }
    assert {
        "processed_messages",
        "classified_email_events",
        "otp_requests",
        "security_events",
    } <= tables
    subprocess.run([*command, "downgrade", "0001_phase1_core"], check=True, env=environment)
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='table'")
        }
    assert {"accounts", "orders", "rentals", "operations", "audit_events"} <= tables
    assert "classified_email_events" not in tables
    subprocess.run([*command, "upgrade", "head"], check=True, env=environment)


def test_phase4_0005_upgrade_downgrade_upgrade(tmp_path):
    database = tmp_path / "phase4-migration.db"
    environment = {**os.environ, "DATABASE_URL": f"sqlite:///{database.as_posix()}"}
    command = [sys.executable, "-m", "alembic", "-c", "alembic.ini"]
    subprocess.run([*command, "upgrade", "0004_phase3_funpay"], check=True, env=environment)
    subprocess.run([*command, "upgrade", "0005_phase4_pixelstorm"], check=True, env=environment)
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("pragma table_info(operations)")}
    assert {"maintenance_login_requested_at", "password_change_requested_at", "security_state", "recovery_claim_token"} <= columns
    subprocess.run([*command, "downgrade", "0004_phase3_funpay"], check=True, env=environment)
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("pragma table_info(operations)")}
    assert "maintenance_login_requested_at" not in columns
    assert "password_change_requested_at" not in columns
    subprocess.run([*command, "upgrade", "head"], check=True, env=environment)
    with sqlite3.connect(database) as connection:
        columns = {
            row[1]
            for row in connection.execute("pragma table_info(classified_email_events)")
        }
    assert {"security_processing_state", "security_claim_token", "security_claimed_at"} <= columns
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='table'")
        }
    assert {"account_lots", "funpay_events", "message_receipts"} <= tables
    subprocess.run([*command, "downgrade", "0003_phase2_security_dispatch"], check=True, env=environment)
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='table'")
        }
    assert "funpay_events" not in tables
    assert "classified_email_events" in tables
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='table'")
        }
    assert {"accounts", "orders", "rentals", "operations", "audit_events"} <= tables
    subprocess.run([*command, "downgrade", "base"], check=True, env=environment)
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='table'")
        }
    assert "accounts" not in tables
    subprocess.run([*command, "upgrade", "head"], check=True, env=environment)


def test_phase5_0006_normal_worker_fencing_upgrade_downgrade_upgrade(tmp_path):
    database = tmp_path / "phase5-fencing-migration.db"
    environment = {**os.environ, "DATABASE_URL": f"sqlite:///{database.as_posix()}"}
    command = [sys.executable, "-m", "alembic", "-c", "alembic.ini"]
    subprocess.run([*command, "upgrade", "0005_phase4_pixelstorm"], check=True, env=environment)
    with sqlite3.connect(database) as connection:
        assert "normal_claim_token" not in {
            row[1] for row in connection.execute("pragma table_info(operations)")
        }
    subprocess.run([*command, "upgrade", "0006_phase5_normal_worker_fencing"], check=True, env=environment)
    with sqlite3.connect(database) as connection:
        assert "normal_claim_token" in {
            row[1] for row in connection.execute("pragma table_info(operations)")
        }
    subprocess.run([*command, "downgrade", "0005_phase4_pixelstorm"], check=True, env=environment)
    with sqlite3.connect(database) as connection:
        assert "normal_claim_token" not in {
            row[1] for row in connection.execute("pragma table_info(operations)")
        }
    subprocess.run([*command, "upgrade", "head"], check=True, env=environment)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0006_phase5_normal_worker_fencing",
        )
