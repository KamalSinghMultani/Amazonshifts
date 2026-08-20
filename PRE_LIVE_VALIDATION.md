# Pre-live validation

This branch keeps the original project and layers the validation/latency work on top:

`watcher.Watcher -> watcher_v2.ScheduleAwareWatcher -> watcher_v3.OptimizedWatcher -> watcher_v4.AutoSessionWatcher -> watcher_v5.PreLiveWatcher`

## 1. Regression suite

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Do not continue to a real hold if the suite is not green.

## 2. Session checks (non-destructive)

Reuse/prove the saved Canada application session:

```powershell
.\.venv\Scripts\python.exe verify_session.py --config config.yaml
```

Exercise a fresh automatic login deliberately:

```powershell
.\.venv\Scripts\python.exe verify_session.py --config config.yaml --force-fresh-login
```

Both commands exit after the check. They do not create an application.

A failure in the background session path now writes a local full-page screenshot and a safe JSON sidecar under `screenshots/`. The directory is gitignored. The sidecar contains structural evidence/key names only, never cookie/storage values, credentials, OTPs, WAF parameters, or solver tokens.

## 3. Normal long-running watcher

```powershell
.\run_watcher.bat
```

`run_watcher.bat` now launches `watcher_v5.py`.

## 4. Hold timing

The v5 direct hold remains browser-driven. It navigates to the schedule-specific application route, lets Amazon's frontend send its own candidate-application calls, and passively watches the `update-application` response.

After `Create Application` is pressed it stops waiting as soon as the observed response proves:

- `currentState == JOB_SELECTED`
- selected `scheduleId` equals the expected schedule
- `softReserveExpirationTimestamp` is present

Every attempt appends a record to:

```text
logs/hold_timings.jsonl
```

The useful measurements are:

- poll start -> hold dispatch
- application navigation commit
- application action ready
- Next click (when present)
- Create Application ready
- Create Application click
- backend reserve confirmation / banner
- poll start -> final result

No auth headers, request bodies, cookies, or tokens are written to this timing file.

## 5. One real Canada-wide validation

Use this only when intentionally ready to create a real application/soft reserve:

```powershell
.\.venv\Scripts\python.exe real_hold_test.py --config config.yaml --minutes 60 --ack-real-hold
```

Properties of this command:

- performs a strong Canada application-session preflight first;
- uses exactly the storage state that passed preflight for the test browser;
- changes no values in `config.yaml`;
- temporarily accepts whatever the already-Canada-scoped API returns;
- runs for at most 60 minutes;
- attempts at most one hold at a time;
- stops immediately after the first confirmed hold;
- also stops after an uncertain result because Create Application may already have succeeded;
- if no hold attempt occurs before the deadline, it exits without creating an application;
- prints the latest hold timing summary when it exits.

A real reservation is still server-side competition. The code can minimize its own delay, but cannot guarantee Amazon will prioritize it over another request that reaches Amazon first.
