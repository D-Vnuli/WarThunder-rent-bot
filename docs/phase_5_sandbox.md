# PHASE 5 sandbox / DRY_RUN

The only runnable PHASE 5 mode is `APP_MODE=SANDBOX` (or `DRY_RUN`) with
`DRY_RUN=true`. `PRODUCTION` is refused by the composition root.

Run the offline demonstration:

```powershell
py -3.14 -m app.sandbox --scenario happy-path
```

It upgrades a temporary application SQLite database through Alembic, then creates
file-backed fake FunPay, Gmail, Pixel Storm and secure-store boundaries. The complete
demo ends with `rental=FINISHED account=AVAILABLE`. No browser, network, OAuth,
FunPay, Gmail or Pixel Storm calls are made.
The application SQLite database contains operational metadata only; credentials and
email payloads remain outside it. The demo prints only readiness and lifecycle state.

`ApplicationRuntime.run_once(now)` is the bounded orchestration entrypoint. It polls
the fake event sources, dispatches durable events, advances scheduler work and performs
startup reconciliation after a restart. The test suite covers duplicate events, blocked
lots, exact buyer OTP delivery, expiry recovery, ambiguous receipts and crash/restart
reconciliation. `SandboxEnvironment.restart()` constructs fresh database, adapter and
application objects over the same fake backend files. `secure-store.vault` and
`email-secrets.vault` are sandbox-only secure-boundary emulations; neither is business
SQLite. Readiness reports safe statuses for DB, FunPay, email pipeline, Pixel Storm,
secure store, lots and rentable account.

Browser tests stay enabled. Install their local test browser when needed:

```powershell
py -3.14 -m playwright install chromium
```

This is an offline integration harness, not a production-ready or live-traffic mode.
