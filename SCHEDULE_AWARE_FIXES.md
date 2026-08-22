# Schedule-aware hold fixes

This branch addresses the main race-condition gaps found during the 2026-08-19 network investigation.

## What changed

### 1. Schedule-level identity

The old watcher deduplicated on `jobId`. That can hide a newly bookable schedule that appears under an already-seen job. `watcher_v2.py` expands matched API job cards through `searchScheduleCards` and treats `scheduleId` as the actionable identity.

### 2. Alert state is separate from reservation state

The old watcher marked a job as seen before attempting the hold. A transient failure could therefore retire that job for the full state TTL.

The new watcher stores two independent keys:

- `alert:schedule:<scheduleId>` — prevents Telegram spam.
- `done:schedule:<scheduleId>` — set only after a confirmed or uncertain reservation, or after alert-only handling.

A failed reservation remains retryable on the next poll while the schedule still reports capacity.

### 3. Schedule preferences run before the hold

`available_days`, `min_hours_per_week`, and `avoid_overnight` are now applied to API schedule cards before a reservation attempt.

### 4. Remove one redundant navigation

When `jobId + scheduleId` are already known, the old `_hold()` first opened the listing and only afterward navigated to the direct application URL. The schedule-aware override goes straight to the application route and keeps the existing browser-driven consent / Create Application flow.

### 5. Existing safety/reliability behavior remains

This branch does **not** replace the Create Application UI flow with a replayed private backend request. It keeps the proven session, login, Playwright, confirmation, screenshot and Telegram machinery intact while reducing avoidable latency around it.

## Running

`run_watcher.bat` now starts:

```text
watcher_v2.py
```

All normal CLI options still work because `watcher_v2.py` delegates to the existing `watcher.main()` after replacing the watcher class.

## Tests

New tests cover:

- schedule ID identity;
- schedule preference filtering;
- overnight rejection;
- minimum-hours rejection;
- preservation of the parent job ID.
