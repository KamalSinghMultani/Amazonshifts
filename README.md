# Amazon Shift Auto-Applier

Watches [hiring.amazon.ca](https://hiring.amazon.ca) for warehouse shifts, pings you on
Telegram the instant a matching one appears, and clicks through to hold the slot —
stopping one step before the final submit so **you** finish the application.

Built because paid bot services snipe these shifts before real applicants can reach them.

---

## Safety model

Three things are true by default, and two of them are hard to turn off by accident:

| | Default | How it changes |
|---|---|---|
| **Clicking** | `dry_run: true` — detects and alerts, never clicks | set `dry_run: false` in `config.yaml` |
| **Final submit** | Never clicked. The bot stops with the submit button on screen | set `hold.stop_before_submit: false` (logs a loud warning) |
| **Credentials** | Never seen by any script. You log in by hand, once | n/a — there is no code path that reads a password |

`auth_state.json` (your session cookies) and `.env` (your Telegram token) are gitignored.
Treat `auth_state.json` as equivalent to your password.

---

## Setup

```bash
python -m venv .venv && .venv\Scripts\activate     # Windows
# source .venv/bin/activate                        # macOS / Linux
pip install -r requirements.txt
python -m playwright install chromium

copy .env.example .env      # then fill in your Telegram bot token + chat id
```

### 1. Log in, once

```bash
python save_session.py
```

Opens a real browser. You type the email, password, and OTP yourself — the script
never reads them. Press Enter in the terminal when you're logged in; cookies are
saved to `auth_state.json` and reused from then on. Re-run this whenever the
session expires.

### 2. Fill in the selectors

`site_selectors.py` ships with placeholders. Get the real ones:

```bash
python -m playwright codegen --load-storage=auth_state.json https://hiring.amazon.ca/app#/jobSearch
```

Click through a job the way the bot should, copy the selectors Playwright generates,
paste them into `SELECTORS`, `HOLD_STEPS`, and `FINAL_SUBMIT`. Then check your work:

```bash
python watcher.py --check-selectors
```

### 3. Say which shifts you want

In `config.yaml`, under `filters:`. All matching is case-insensitive substring
matching, and an empty `include_*` list means "anything passes".

```yaml
filters:
  include_titles: ["warehouse", "sortation"]
  exclude_titles: ["seasonal"]
  include_locations: ["brampton", "mississauga"]
  exclude_schedules: ["night"]
  min_pay_rate: 18.00
```

### 4. Run it

```bash
python watcher.py --once     # single poll, good for a first look
python watcher.py            # the real loop, still dry-run
```

Watch it for a while in dry run. When the alerts look right, set `dry_run: false`.

---

## Detection modes

Set `polling.mode` in `config.yaml`.

### `dom` (default)

Reloads the job search page each poll and scrapes the rendered HTML. Needs no
setup beyond the selectors, but a page load costs seconds — and seconds are the
whole game against bots that grab shifts in under one.

### `api` (faster)

Calls the JSON endpoint the page itself calls, through Playwright's
`context.request`, which reuses your session cookies automatically. Measured at
~60ms per poll versus seconds for a page load. The browser page is only opened
once a match is found, and **the Telegram alert is sent before that happens** —
so you hear about the shift at the moment the bot does, not after it finishes
navigating.

To set it up:

```bash
python api_sniffer.py
```

Browse the site normally in the window that opens. Every JSON request is logged
to `api_captures/`; start with `api_captures/index.md`, find the one carrying the
job list, and copy its URL, method, body, and headers into the `api:` block of
`config.yaml`. Then set `polling.mode: api`.

> ⚠️ If that request needs an `authorization: Bearer …` header, note that tokens
> expire and rotate — unlike cookies, which Playwright refreshes for you. A pasted
> token works for a while and then starts returning 401. The sniffer flags any
> request where it sees one. If you hit this, `dom` mode is the reliable fallback.

`api` mode can detect shifts without any selectors configured — but holding a slot
still needs them, since holding means clicking real buttons.

---

## Running it

```bash
python watcher.py                    # config.yaml, dry run unless configured otherwise
python watcher.py --once             # one poll, then exit
python watcher.py --live             # force dry_run: false for this run only
python watcher.py --check-selectors  # list unconfigured selectors
python watcher.py --config other.yaml
```

Ctrl-C shuts down cleanly, saving state first. Logs go to `logs/watcher.log`
(rotated) and to the terminal.

---

## How it behaves

- **Polling** is randomized: `interval_seconds` plus a random `0..jitter_seconds`,
  so the traffic isn't a metronome. Intervals under 5s are rejected outright.
- **Dedup** is persistent (`state/seen_shifts.json`), so a shift alerts once, not
  once per poll. Entries expire after `state.ttl_hours` so a genuinely re-posted
  shift can alert again. A shift is marked seen *before* the hold is attempted —
  better to miss a retry than to spam the same alert every 20 seconds.
- **Circuit breaker**: after `max_consecutive_errors` failures in a row it sleeps
  for `cooldown_seconds` and tells you on Telegram.
- **Notifications never take down the watcher.** Every send is wrapped; a dead
  network or a bad token degrades to a log line.
- **Session expiry** is detected by watching for a redirect to a login URL, and
  reported on Telegram.

---

## Tests

```bash
python -m pytest -q
```

37 tests, no browser and no network needed. They cover filter logic, dedup and TTL,
JSON parsing against schema changes, config validation, notification retry, and —
with fake DOM objects — the card-matching logic.

Several are regression tests for bugs found during the build, and are worth
keeping:

- `test_module_is_not_named_selectors` — this module was once called `selectors.py`,
  which shadows the stdlib `selectors` module that asyncio (and therefore Playwright)
  imports. It crashed at startup with a confusing traceback.
- `test_find_matching_card_picks_the_right_card_by_id` — `hold_shift()` originally
  grabbed the first job card on the page regardless of which one matched, so it
  would cheerfully hold the wrong shift.
- `test_notifier_sends_photo_bytes_so_retries_are_replayable` — `sendPhoto` passed
  an open file handle to `requests`; the handle is consumed by the first attempt,
  so every retry uploaded an empty file.
- `test_extract_shifts_makes_relative_hrefs_absolute` — card hrefs come back
  relative (`/job/123`), which `page.goto()` cannot navigate to.

---

## Files

| File | Purpose |
|---|---|
| `watcher.py` | Main loop, circuit breaker, CLI |
| `config.py` | Config loading, defaults, validation, logging setup |
| `config.yaml` | All settings |
| `site_selectors.py` | Every CSS selector + the DOM click flow |
| `api_client.py` | JSON endpoint polling and parsing |
| `api_sniffer.py` | Discovers that endpoint |
| `shift_matcher.py` | `Shift` model, stable ids, filter matching |
| `state_store.py` | Persistent "already alerted" set |
| `notifier.py` | Telegram, with retry/backoff |
| `save_session.py` | One-time manual login |
| `tests/test_smoke.py` | The test suite |
| `PROJECT_CONTEXT.md` | Background, decisions, and what's left |

---

## Status

Verified end-to-end against a local mock server (both modes, with a real browser).
**Not yet run against hiring.amazon.ca** — see `PROJECT_CONTEXT.md` for exactly
what remains.

Use this for your own applications, at a polite polling rate. It exists to put you
on even footing with the paid snipers, not to hammer the site.
