# Operator Runbook

## Normal operation

1. Run `python -m app.manage migrate` after reviewing the release.
2. Run `python -m app.manage preflight` and resolve every `NOT_READY` result.
3. Inspect `python -m app.manage status`.
4. Start the runtime in the explicitly selected mode. Use `python -m app.main --dry-run`
   for production-host validation without live external actions.
5. Use the service shutdown path (SIGINT/SIGTERM where available); do not delete
   the business database or lock file manually.

## Routine checks

- `db-check` reports SQLite integrity and foreign-key results without repair.
- `backup` creates a consistent business-DB backup. Verify it with `backup-check`.
- `diagnostics --output ...` creates a redacted support bundle; do not add vaults,
  cookies, raw emails, or browser artifacts to it.

## Incidents

- `MANUAL_REVIEW`: do not enable lots or send credentials. Inspect status and the
  associated operation/rental identifiers, then follow the approved manual process.
- FunPay/Gmail/Pixel Storm `AUTH_REQUIRED`, `PIXEL_PASS_REQUIRED`, or challenge:
  treat the affected capability as unavailable; do not bypass security controls.
- Worker failure: inspect status and diagnostics. Repeated failures are backed off
  and must be resolved before claiming readiness.
- Database integrity failure: stop the runtime, preserve the database, run
  `db-check`, validate a known backup, and restore only through an approved manual
  process. The application never repairs corruption automatically.

Never place real tokens, passwords, OTPs, reset URLs, cookies, or browser storage
in command output, logs, diagnostics, or support tickets.
