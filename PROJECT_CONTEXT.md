# Project context

Written so this project can be picked up in a fresh session — or by a fresh
Claude Code session — without re-explaining any of the background. If you are an
assistant reading this: everything under "Decisions" is settled. Don't relitigate it.

Last updated: 2026-08-17

---

## The goal

Paid "scammer" bot services snipe Amazon warehouse shifts on hiring.amazon.ca
before real applicants can grab them. Rather than pay one of those services,
build the equivalent tool.

The bar isn't "eventually notices a shift." It's "notices and reacts fast enough
to compete with a bot," which is what drove the `api` detection mode.

---

## Decisions (settled — do not relitigate)

1. **Alert on Telegram the instant a matching shift appears.** The alert fires
   before any page load or click, so latency to the human is minimal.
2. **Auto-click to hold the slot, but stop one step before final submit.** The
   user finishes by hand. This was chosen deliberately over full auto-submit.
   Full automation exists behind `hold.stop_before_submit: false`, and is only to
   be enabled on explicit request.
3. **Never touch password or OTP.** One manual login via `save_session.py`, then
   the session is reused. There is no code path that reads a credential.
4. **Safe by default.** `dry_run: true` ships enabled, and stays that way until
   the user has watched it run and trusts it.
5. **Polite polling.** Randomized interval; intervals under 5s are rejected by
   config validation.

---

## Architecture

Python + Playwright (sync API).

```
watcher.py ──┬── api mode ──> api_client.py ──> context.request ──> JSON endpoint
             └── dom mode ──> site_selectors.py ──> page.reload() + scrape
                     │
                     ├──> shift_matcher.py   (Shift model, stable id, filters)
                     ├──> state_store.py     (persistent dedup, TTL)
                     └──> notifier.py        (Telegram, retry/backoff)
```

**Two detection modes**, via `polling.mode`:

- `dom` — reload the page, scrape rendered HTML through `site_selectors.py`.
  No setup beyond selectors, but a page load costs seconds.
- `api` — hit the site's own JSON endpoint directly via Playwright's
  `context.request`, which reuses the browser context's cookies automatically.
  Measured at ~60ms/poll against a local mock. The page is only loaded once a
  match is found, and the alert is sent *before* that.

**Endpoint discovery**: `api_sniffer.py` logs every JSON XHR/fetch response plus
request headers to `api_captures/`, with an `index.md` summary. It flags requests
carrying `authorization` / `x-api-key` headers, because those expire and rotate —
unlike cookies, which Playwright refreshes. A pasted bearer token will work and
then silently start 401ing. If that happens, `dom` mode is the fallback.

**Identity/dedup**: `Shift.stable_id` prefers the site's own job id; without one
it hashes title + location + schedule. `state_store.py` persists the seen set as
JSON with atomic writes (tmp + `os.replace`) so a crash mid-write can't cause a
re-alert storm. Entries expire after `state.ttl_hours`.

---

## Current state

Everything is built and tested. 37 unit tests pass with no browser or network.
Both modes were verified end-to-end against a local mock HTTP server driving a
real Chromium instance:

- **api mode**: 3 shifts fetched → filters applied → 1 matched → alerted → state
  persisted → second run correctly alerted 0 (dedup works).
- **dom mode**: 3 cards extracted with all fields → `hold_shift()` on the *third*
  card opened the third card (not the first) → the submit button was **not**
  clicked → with `stop_before_submit: false` it did click submit → a shift not on
  the page failed cleanly instead of raising.

### Live findings — 2026-08-17, first real run against hiring.amazon.ca

Login now works and the session is valid. Four things were learned that changed
the code:

1. **Bot detection at login.** Playwright's bundled Chromium got as far as the
   OTP step and then silently refused. Fixed with real Chrome (`channel`), a
   persistent profile (`user_data_dir`), and `stealth`. Verified: `navigator.
   webdriver` undefined, UA `Chrome/151.0.0.0`, `window.chrome` present.

2. **Headless leaks `HeadlessChrome` in the UA — and CloudFront blocks on it.**
   This is the important one. Overriding `navigator.userAgent` from JS is *not*
   enough, because the CDN inspects the request header. `resolve_user_agent()`
   probes the browser and strips the marker at context level.

3. **The WAF is real and it bites.** Three full page loads ~14s apart returned
   `403 ERROR / Request blocked` from CloudFront. Defaults were moved from
   20s ± 8s to **45s ± 20s**, and a block now raises so the circuit breaker backs
   off. Do not lower the interval without watching the logs.

4. **The access token expires but self-heals.** The first load after a while
   shows "Token expired. Try refreshing the browser." A single reload clears it —
   verified. The watcher does that reload automatically and does not treat it as
   an error.

Also confirmed live:

- `no_results` is `#jobNotFoundContainer` — now filled in, no longer a placeholder.
- Job cards live inside `[data-test-id='jobResultContainer']` (recorded as
  `RESULTS_CONTAINER`). The individual card selector could **not** be captured,
  because no jobs were posted at the time.
- The site ships a **CAPTCHA modal** (`[data-test-id='captchaModal']`), hidden in
  the DOM on every load. Only visibility counts. Detected as its own state.
- CSS classes are Emotion hashes (`hvh-careers-emotion-1ua2ui2`) and **will rot**.
  Use `data-test-id` / `id` when filling in the rest.

The single most important structural lesson: a WAF block, a CAPTCHA, and an
expired token all render as a normal HTTP 200 page with zero job cards. Without
`page_state()` classifying them, the watcher would report "no shifts available"
forever while being completely broken. Any future change here must preserve that
distinction.

---

## Bugs found and fixed during the build

Kept here because each has a regression test, and because they're the kind of
thing that would be reintroduced by someone refactoring innocently.

1. **`selectors.py` shadowed the stdlib.** The DOM module was originally named
   `selectors.py`, which shadows Python's stdlib `selectors` module — imported by
   asyncio, and therefore by Playwright. It crashed at startup with a confusing
   traceback. Renamed to `site_selectors.py`.
   → `test_module_is_not_named_selectors`

2. **`hold_shift()` held the wrong shift.** It grabbed the first job card on the
   page regardless of which one matched. Fixed with `find_matching_card()`, which
   matches on the site's job id first and falls back to exact title + location.
   → `test_find_matching_card_picks_the_right_card_by_id`

3. **Telegram photo retries uploaded zero bytes.** `sendPhoto` passed an open file
   handle to `requests`; the handle is consumed by the first attempt, so every
   retry uploaded an empty file. Now reads bytes once and replays them.
   → `test_notifier_sends_photo_bytes_so_retries_are_replayable`

4. **Card URLs were relative.** `extract_shifts()` returned hrefs like `/job/123`
   straight from the DOM, which `page.goto()` cannot navigate to. Now resolved
   against `page.url` with `urljoin`. Caught by the DOM end-to-end run.
   → `test_extract_shifts_makes_relative_hrefs_absolute`

---

## What's left

In order. Steps 1–3 need a live session and can't be done without one.

### 1. Real CSS selectors — required (~10 min)

`site_selectors.py` ships with `TODO` placeholders.

```bash
python save_session.py     # if you haven't already
python -m playwright codegen --load-storage=auth_state.json https://hiring.amazon.ca/app#/jobSearch
```

Click through a job the way the bot should. Fill in:

- `SELECTORS` — `job_card` and the per-card field selectors. `card_id_attr` is the
  attribute holding Amazon's own job id; getting this right is what makes dedup
  and card-matching reliable, so find it if it exists.
- `HOLD_STEPS` — the ordered click path from job card to the submit screen. Step 1
  is scoped to the matched card; later steps run page-wide.
- `FINAL_SUBMIT` — the button that is **never** clicked while
  `stop_before_submit` is true. Load-bearing safety, not decoration.

Verify with `python watcher.py --check-selectors`.

Note that `no_results` matters more than it looks: without it, "no shifts posted"
and "our selectors rotted" are indistinguishable, and the bot would report a
confident zero while silently broken. `extract_shifts()` already logs a warning
for that case.

### 2. First live run — required

```bash
python watcher.py --once     # look at what it found
python watcher.py            # let it run, still dry_run: true
```

Sit in dry run for a meaningful stretch — ideally across a window when shifts
actually drop. Check that the shifts it reports match what you see in the browser,
and that the filters are neither too loose nor too tight. Only then set
`dry_run: false`.

### 3. API endpoint — optional, but it's the speed win

```bash
python api_sniffer.py
```

Fill in the `api:` block of `config.yaml` from `api_captures/index.md`, then set
`polling.mode: api`. Watch for the bearer-token caveat above.

Sanity check: run `api` mode and `dom` mode against the same page and confirm they
report the same shifts. If they disagree, `shifts_path` or `field_map` is wrong.

### 4. Deployment — open question

Currently it runs in a terminal for as long as that terminal is open. If shifts
drop at unpredictable hours, this wants to be a long-running service:
a scheduled task / `systemd` unit / `screen` session on an always-on box, with the
session refreshed when it expires. Not built yet — flag it when the basics work.

---

## Things a future session should know

- **Session expiry is the main operational failure.** The watcher detects a
  redirect to a login URL and reports it on Telegram, but recovery is manual —
  re-run `save_session.py`. If this turns out to happen often, automatic detection
  plus a "your session died" alert loop is the next thing to build.
- **Selectors will rot.** Amazon ships frontend changes. The `no_results` marker is
  what distinguishes "no shifts" from "we're broken"; keep it configured.
- **The screenshot on hold is genuinely useful.** When a hold half-works, the
  screenshot in Telegram is usually the fastest way to see which step went wrong.
- **`interval_seconds` is a tradeoff, not a dial to max out.** Faster polling wins
  races but is more conspicuous and risks rate-limiting. 20s ± 8s is a starting
  point, not a tuned value.
- **`raw` on every `Shift`** holds the original API payload. When a field maps
  wrong, that's where to look.

---

## What the competitor's own guide told us (2026-08-18)

`full_competer_workflow.pdf` is their published setup guide, and it answered
questions our own testing could not:

- **"To stay active, the bot auto-logs in every 2 hours."** The ~2h session
  ceiling is real, not an artefact of our own tests. Keeping a session alive
  is not the strategy — replacing it before it dies is. That is now
  `session.relogin_every_seconds`, at 100 minutes.
- **"For OTP, you can set auto-forwarding (best)."** They forward Amazon's mail
  to `otp@amazonwarehousejobs.ca`, a domain they own. So they need the inbox
  too — there is no secret that avoids the emailed code. `otp_mail.py` is the
  same mechanism, minus the forwarding hop.
- **They fill in basic info and the assessment after picking**, leaving the
  human 3 hours for the background check and interview slot. We deliberately
  stop at the hold: those steps are SIN, date of birth and address history.

## Deferred: schedule-aware matching

Their onboarding collects preferences we cannot express yet:

| They filter on | We have |
|---|---|
| warehouse type (Delivery / Fulfilment / Sortation / XL) | title substrings only |
| minimum shift hours (3h / 7h / 10h) | nothing |
| availability by day and time | nothing |
| shift type (Flex / Part / Reduced / Full) | matched loosely via title |

**There is a real bug hiding behind this.** `HOLD_STEPS`' "pick a shift" step
clicks the FIRST Apply button in the schedule flyout. A job with several
schedules can therefore hand you the wrong one — their own pick message shows
`Sat, Sun, Mon, Tue 8:00 PM - 6:30 AM (40h)`, which is exactly the sort of
overnight block someone wanting days would not want.

Everything needed to fix it already exists: `schedules.py` fetches
`searchScheduleCards` for a jobId, which returns `scheduleId`, real shift text,
`hoursPerWeek`, `siteId` and `laborDemandAvailableCount`. The work is to filter
those against preferences and click the matching card's Apply rather than the
first one.

Not attempted: deep-linking to `/application/?jobId=…&scheduleId=…`. Tested six
ways on 2026-08-18 and every one bounces to the login page. See the comment on
`hold.direct_apply` in `config.py` before trying again.
