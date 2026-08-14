# PHASE 6 — Production Hardening

The production host wraps the accepted PHASE 0–5 domain, repository, operation,
reconciliation, and lease/fencing implementation. It does not duplicate rental
business logic.

## Modes and gates

- `SANDBOX` uses persistent fake services only.
- `PRODUCTION_DRY_RUN` uses the production host with offline blocked transports.
  A blocked mutation returns a safe failed result; it is never reported as a fake success.
- `PRODUCTION` requires both `DRY_RUN=false` and `ALLOW_LIVE_OPERATIONS=true`.
  Pixel Storm and FunPay mutation gates are separate and default to false.

Credentials never enable production by themselves. Settings summaries, logs,
CLI output, diagnostics, and errors redact configured secret values.

## Startup and shutdown

Startup order is: validate settings; configure safe logging; acquire the
machine-local lock; open/check SQLite; verify Alembic head; initialize stores;
preflight and consistency checks; startup reconciliation; readiness; workers.
Missing migrations or a failed integrity/consistency check fail closed before a
worker is started.

Shutdown first stops acceptance of new work, then closes bounded resources,
database resources, and the single-instance lock. Durable operation records are
never deleted by shutdown. Existing SQLite claims, tokens, and side-effect lease
heartbeats remain the concurrency authority; the runtime lock only prevents an
operator accidentally starting two local hosts.

## Operator commands

- `python -m app.manage migrate`
- `python -m app.manage preflight`
- `python -m app.manage status`
- `python -m app.manage db-check`
- `python -m app.manage backup`
- `python -m app.manage backup-check <file>`
- `python -m app.manage diagnostics --output diagnostics.zip`
- `python -m app.main --dry-run`

Backups use SQLite's backup API and contain only the business database. They do
not copy secure stores, browser session vaults, cookies, OAuth tokens, or email
payload vaults. Backup checking restores only into a temporary database.

## Operational safety

Poller failures use bounded exponential backoff and degrade health. Notification
delivery is secondary and cooldown-deduplicated. Diagnostics contain safe
settings, readiness, revision, and metadata summaries only. Metadata retention
removes only old completed non-essential records; active and recovery work is
preserved. Browser debug artifacts are disabled by default, and browser contexts
are explicitly closed.

`MANUAL_REVIEW` remains fail closed: lots stay off, OTP and credential delivery
remain closed, and status asks the operator to review the durable record.

LIVE EXTERNAL VALIDATION HAS NOT BEEN EXECUTED.
