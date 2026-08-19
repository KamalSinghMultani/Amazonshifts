# Optimization pass 2

This pass closes the remaining gaps found after the schedule-aware PR review.

## What changed

### Detection no longer waits for session maintenance

The old main loop called session checks and re-login synchronously after each
poll. A slow OTP or challenge could therefore create a long detection hole.

`watcher_v3.py` launches `session_refresh.py` as a separate process. The main
watcher keeps its normal API polling cadence while the helper checks or refreshes
the hiring session. On success the main watcher imports the refreshed cookies,
reloads its token page, and persists the merged storage state.

The helper deliberately uses an isolated non-persistent browser context, so it
does not try to open the same Chrome profile that the live watcher owns.

### Successful proactive refreshes no longer consume the safety budget

A 100-minute cadence needs more than 12 successful refreshes in 24 hours. The
old counter treated success and failure the same, which could disable refreshes
before the day ended.

The second-pass watcher interprets the existing `max_relogins_per_day` setting
as a cap on *failed* background refreshes. Successful health checks and
successful proactive refreshes do not consume it. CAPTCHA still blocks further
automatic attempts until the watcher is restarted or the next-day reset clears
the block.

### Large job drops use batched schedule queries

`searchScheduleCards` requires a jobId, but GraphQL aliases allow several jobIds
inside one HTTP request. `schedule_batch.py` chunks matching jobs and asks for
many schedule-card sets per request instead of making one HTTP round trip for
every matching job.

If Amazon rejects the aliased document, the watcher falls back to the already
proven per-job `fetch_schedules()` behavior for that poll.

### Direct application is now an optimization, not a single point of failure

The direct `jobId + scheduleId` application route is still tried first because
it removes job-detail/flyout navigation. If that route fails, `watcher_v3.py`
retries through the normal listing/schedule Apply flow with direct mode
temporarily disabled.

This is specifically for the observed case where a direct application URL can
redirect differently from the ordinary Apply flow under some sessions.

### Backend soft-reserve verification

`hold_verify.py` listens to the browser-driven
`candidate-application/update-application` response. When Amazon returns all of:

- `currentState == JOB_SELECTED`
- the expected `jobScheduleSelected.scheduleId`
- a non-null `softReserveExpirationTimestamp`

that response is treated as authoritative evidence of a soft reserve even if
the React hold banner has not rendered yet.

The code does **not** replay the candidate-application request itself; Amazon's
own frontend still performs the Create Application action.

## Current critical path

```text
searchJobCardsByLocation
        ↓
local job filters
        ↓
batched searchScheduleCards
        ↓
capacity + schedule preferences
        ↓
direct application route
        ↓
Create Application
        ↓
update-application observed
        ↓
soft reserve confirmed
```

Session health/re-login runs beside this flow rather than inside it.

## Test before merge

The remaining live-only proof is the Canadian direct application route. The
fallback path is there specifically so one failed direct route should not throw
away the schedule. During the first live test, capture these log lines:

- poll latency;
- batched schedule lookup success/fallback;
- `holding schedule ... via direct application URL`;
- `backend soft reserve confirmed` if returned;
- `direct application route failed ... trying proven click path` if fallback is used;
- background session-maintenance start/result.

Do not merge until one real Canadian application confirms the path you want to
keep enabled.
