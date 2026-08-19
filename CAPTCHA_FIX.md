# The CAPTCHA is invisible to the detector — what's actually wrong

**Status:** the login reaches the challenge every time and stops there. The
solver has never been called. Not once, all night.

---

## What we know for certain

Measured tonight, not inferred:

| Fact | Evidence |
|---|---|
| A CAPTCHA is on screen | `screenshots/relogin-unknown-20260819-002921.png` — "Choose all the bags", 3×3 grid, Confirm button |
| Its words are NOT in the page text | the same failure logged `the screen said: ... Where should we send your verification code? ...` — no challenge wording at all |
| It is NOT in an `awswaf` iframe | `captcha_frame()` was live in that run and found nothing; no `CAPTCHA_DETECTED` line was logged |
| A clean login page has no `awswaf` frame either | frame census: only `auth.hiring.amazon.com` and a hidden `demdex.net` analytics iframe |
| The AWS WAF SDK *is* loaded | request capture shows `ebcec29959ba.edge.sdk.awswaf.com/.../inputs` and `/mp_verify` |

So: the challenge exists, is rendered by AWS WAF's SDK, and lives somewhere
that neither `page.inner_text("body")` nor `page.frames` can see.

## The remaining explanation

**Shadow DOM.** The WAF SDK injects a custom element into the main document and
renders the puzzle inside its shadow root.

That fits every observation:

- `inner_text("body")` does **not** include shadow content → no wording
- there is no iframe → `page.frames` shows nothing
- Playwright **locators do pierce open shadow roots** → the tiles are reachable,
  just not the way we have been looking

This is a hypothesis with one piece of evidence still missing, which is why the
next step is to confirm rather than build on it.

---

## Step 1 — confirm it (no code changes, one login)

`watcher.py` now dumps a DOM census whenever a re-login fails. On the next
failure the log will contain:

```
frames on the failed page (2): https://auth.hiring.amazon.com/#/login | https://amazonhr.demdex.net/...
iframes: [{'src': '...', 'title': '...'}]
shadow hosts: [{'tag': '...', 'id': '...', 'cls': '...', 'text': "Let's confirm you are human Choose all the bags..."}]
```

**Read the `shadow hosts` line.** The entry whose `text` contains the challenge
wording is the host element. Its `tag` is the selector you need.

Just restart the watcher and let one attempt fail:

```
.\run_watcher.bat
```

## Step 2 — make detection see it

**File: `relogin.py`, class `StateDetector`.**

`captcha_frame()` and the frame branch of `detect_captcha_type()` are correct
for an iframe challenge and cost nothing — keep them as a fallback. What needs
adding is a shadow-DOM path:

- **`_get_text()`** — `page.inner_text("body")` cannot see shadow content.
  Either add the shadow text (via an `evaluate` that walks `shadowRoot`s), or
  stop relying on text for this challenge and key off the host element instead.
- **`detect_captcha_type()`** — if the host element from Step 1 is present and
  visible, return `IMAGE_GRID`.
- **`_is_image_grid()`** — count tiles with a Playwright locator scoped to the
  host. Locators pierce open shadow roots; `inner_text` does not.

Detection is the whole blocker. Once `detect_state()` returns
`CAPTCHA_REQUIRED`, everything downstream already works:
`_transition_to_next` → `_solve_captcha` → `self.solver.solve(...)`.

## Step 3 — the solver itself

**File: `relogin.py`, class `TwoCaptchaSolver`.** Three stubs still
`return False`: `_solve_image_grid`, `_solve_token`, `_solve_text`. That is
yours to write.

Two constraints that come out of tonight:

- If the challenge is in a shadow root, `page.locator(...)` reaches it, but
  `page.frame_locator(...)` does not — there is no frame.
- `_solve_captcha` already passes `frame=` (currently `None` for a shadow-DOM
  challenge). The host element is the more useful handle.

---

## What is already fixed and working

Do not re-debug these; they are done and tested:

| Fixed | Was |
|---|---|
| Logs in to **Canada** | read the country from `page.url`, which is always the `.com` auth domain → every login signed into the US site |
| Loads the country site first | went straight to the auth domain, which returns you to the US site |
| Types the code with key events | `fill()` left the field empty as far as the app was concerned |
| Presses Verify **and** Continue | one press leaves you on the same screen, looking exactly like a rejected code |
| `_is_authenticated` does not navigate | it did, and steered the flow off the login form mid-login |
| No success claimed in 162ms | it was declaring victory on the pre-submit page |
| Re-login attempts are capped | the expiry path was uncapped: ~12 logins in 90 minutes |
| Reads the code from the inbox | works — 11-13s from request to code in hand |

## Also worth knowing

- **`relogin_blocked`**: a `CAPTCHA` status disables scheduled re-logins until
  restart. Deliberate — a challenged account should not be hammered.
- **Detection and holding are unaffected.** The watcher polls Canada every 3s
  regardless of session, and alerts within seconds. Only the *hold* needs a
  session.
- **A manual `python save_session.py` sidesteps all of this** for ~2 hours: you
  solve one challenge by hand and the watcher can hold shifts immediately.
