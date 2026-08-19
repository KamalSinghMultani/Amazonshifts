"""All DOM knowledge about hiring.amazon.ca lives here.

⚠️  THE SELECTORS BELOW ARE PLACEHOLDERS. Fill them in before going live:

      python -m playwright codegen --load-storage=auth_state.json \
             https://hiring.amazon.ca/app#/jobSearch

    Click around, copy the selectors Playwright generates, paste them in.
    `python watcher.py --check-selectors` tells you what is still unset.

NOTE ON THE FILENAME: this module must NOT be called `selectors.py`. That name
shadows Python's stdlib `selectors` module, which asyncio — and therefore
Playwright — imports at startup. Naming it that crashes the whole program on
import with a confusing traceback.
"""

from __future__ import annotations

import logging
import time
from typing import Any, NamedTuple
from urllib.parse import urljoin

import schedules
from shift_matcher import Shift

log = logging.getLogger(__name__)

# Sentinel: any selector still equal to this is unconfigured.
TODO = "TODO"

# CONFIRMED against live job cards on hiring.amazon.com, 2026-08-17 (the CA
# site had no jobs posted; both run the same "HVH careers" frontend, so these
# transfer). A real card looks like:
#
#   [data-test-id="JobCard"]  role="link", no href, no id attribute
#     Featured
#     Robotics Warehouse Associate      <- first <strong>, no test-id
#     3 shifts available
#     Type: Full Time
#     Duration: Seasonal                <- jobCardDurationText
#     Pay rate: Up to $23.50            <- jobCardPayRateText
#     Troutdale, OR                     <- last line, no test-id
#
# An empty string means "no selector exists — use the fallback in code".
SELECTORS: dict[str, str] = {
    # A container that wraps every job/shift card in the results list.
    "job_card": "[data-test-id='JobCard']",

    # Fields read from *within* one job_card.
    # No job id is exposed anywhere on the card, so dedup falls back to the
    # title+location+schedule hash in Shift.stable_id. `api` mode does expose
    # real job ids — another reason to prefer it once configured.
    "card_id_attr": "",
    # The title has no test-id. It is the first <strong>; the later ones are
    # the "Type:" / "Duration:" / "Pay rate:" labels.
    "card_title": "strong",
    # No test-id and no stable class — see _card_location() for the fallback.
    "card_location": "",
    # The card shows Duration/Type, not shift times. Real shift schedules only
    # appear on the job detail page, so filter on those with care.
    "card_schedule": "[data-test-id='jobCardDurationText']",
    "card_pay": "[data-test-id='jobCardPayRateText']",
    # Cards are role="link" with a JS click handler and no href, so there is
    # no URL to extract.
    "card_link": "",

    # Shown when there are simply no jobs. Used to tell "empty" apart from
    # "our selectors broke", which otherwise look identical.
    # CONFIRMED against the live site 2026-08-17, while no jobs were posted:
    # <div id="jobNotFoundContainer"> wrapping "Sorry, there are no jobs
    # available that match your search."
    "no_results": "#jobNotFoundContainer",
}

# Confirmed live: the results list lives inside
# [data-test-id='jobResultContainer']. Individual job cards could not be
# identified because no jobs were posted at the time — scope your codegen
# hunt for `job_card` to inside that container.
RESULTS_CONTAINER = "[data-test-id='jobResultContainer']"

# ⚠️ When filling in selectors, prefer data-test-id / id over CSS classes.
# This site is built with Emotion, so classes look like
# `hvh-careers-emotion-1ua2ui2` — that hash changes whenever Amazon rebuilds
# the frontend, and any selector using it will silently rot.

class HoldStep(NamedTuple):
    """One click in the path to a held slot.

    `opens_popup` exists because waiting for a new tab is expensive and only
    one step actually opens one. Measured on a real hold: waiting after every
    step burned 8 seconds twice — 16 of the 28 seconds between spotting the
    shift and having it reserved, spent watching for tabs that were never
    going to appear.
    """

    label: str
    selector: str
    opens_popup: bool = False


# The click path to hold a slot, in order.
# Everything here IS clicked when dry_run is false.
# ":scope" means "the matched card itself" — job cards are role="link" divs
# with a JS click handler, so the card IS the button.
HOLD_STEPS: list[HoldStep] = [
    # CONFIRMED: the card is role="link" — clicking it opens
    #   /app#/jobDetail?jobId=JOB-US-0000018024
    HoldStep("open job", ":scope"),
    # CONFIRMED: the detail page's primary action. Opens the schedule flyout
    # ([data-test-id='scheduleSelectorPanelFlyout']) in the SAME tab.
    HoldStep("select schedule", "[data-test-id='jobDetailSelectScheduleButton']"),
    # CONFIRMED live 2026-08-17: each schedule card in the flyout carries its
    # own "Apply" button. Despite the name it is a <button>, not a link.
    #
    # ⚠️ The one step that OPENS A NEW TAB. Everything after it runs there.
    HoldStep("pick a shift", "[data-test-id='ScheduleCardSelectScheduleLink']",
             opens_popup=True),
    # CONFIRMED: the application opens on a pre-consent page whose only action
    # is "Next" — same tab. It commits nothing, it just reveals the consent
    # screen where the real decision lives.
    HoldStep("open the consent screen", "[data-test-id='layout'] button:has-text('Next')"),
]

# That is the whole click path, and it is deliberately short.
#
# CONFIRMED live 2026-08-17, signed in: clicking Apply opens
#   /application/us/?country=us&jobId=JOB-…&locale=en-US&scheduleId=SCH-…
# titled "Your journey to becoming an Amazon Associate starts here."
#
# The scheduleId in that URL is the point of the whole exercise: the specific
# shift is now attached to an open application, which is what "holding" means
# here. What follows is a multi-page application — consent, personal details,
# background-check authorisation — needing information this program does not
# have, so the watcher stops here and hands over.

# The schedule flyout, so "pick a shift" can be scoped to it rather than
# matching a stray Apply button elsewhere on the page.
SCHEDULE_FLYOUT = "[data-test-id='scheduleSelectorPanelFlyout']"

# Marks a job detail page. Used to tell "we are on the results list" from
# "we already navigated straight to the job", which matters because api mode
# jumps directly to the detail URL and there are no cards there to click.
DETAIL_PAGE_MARKER = "[data-test-id='jobDetailSelectScheduleButton']"

# The detail page DOES expose the job id ([data-test-id='jobDetailInfoJobId'],
# and it is in the URL), even though the search cards do not. So in api mode,
# where real job ids are available, set config api.url_template to
#   https://hiring.amazon.ca/app#/jobDetail?jobId={id}
# and the watcher can jump straight to the job instead of hunting for a card.

# THE one click that commits you, and the one that actually holds the shift.
#
# The consent screen states you are 18+, willing to take a drug test, and
# agree to the data policy. Pressing Create Application accepts all of that
# and reserves the slot — confirmed by the banner it produces:
#
#   "We are holding a spot for you for the next 2 hours and 59 minutes to
#    complete the remaining steps."   (then Step 1 of 7)
#
# So this is deliberately NOT part of HOLD_STEPS. It is clicked only when
# hold.stop_before_submit is false, exactly like the old final-submit guard:
# everything before it is reversible browsing, this is not.
CREATE_APPLICATION: str = (
    "[data-test-id='layout'] button:has-text('Create Application')"
)

# Proof the spot is really held, rather than us assuming it from a click that
# appeared to work. Read back and passed on to Telegram with its countdown.
HOLD_CONFIRMATION_PATTERN = r"holding a spot[^\n]*"

# Marks the application page, so a hold can prove it landed where it meant to
# rather than reporting success from whatever page it happens to be on.
APPLICATION_PAGE_MARKER = "[data-test-id='text-pre-consent-page-title']"

# The application mounts either the pre-consent screen (Next) or, if that step
# is skipped, the consent screen (Create Application). Either one proves the
# app is up and ready to be clicked.
APPLICATION_ANY_ACTION = (
    "[data-test-id='layout'] button:has-text('Next'), "
    "[data-test-id='layout'] button:has-text('Create Application')"
)
APPLICATION_URL_MARKER = "/application/"


def unconfigured_detection() -> list[str]:
    """Placeholders that stop the watcher from *seeing* shifts."""
    return [name for name, sel in SELECTORS.items() if sel == TODO]


def unconfigured_hold() -> list[str]:
    """Placeholders that stop the watcher from *clicking* through to a slot."""
    missing = [
        f"HOLD_STEPS[{i}] {step.label}"
        for i, step in enumerate(HOLD_STEPS)
        if step.selector == TODO
    ]
    if CREATE_APPLICATION == TODO:
        missing.append("CREATE_APPLICATION")
    return missing


def unconfigured() -> list[str]:
    """Names of every selector still left as a placeholder."""
    return unconfigured_detection() + unconfigured_hold()


def detection_ready() -> bool:
    """Enough is configured to detect and alert.

    Deliberately separate from selectors_ready(): the last two hold steps can
    only be captured by starting a real application, so requiring them before
    the watcher will run at all would block the dry-run period that is supposed
    to come *first*. Detecting and alerting needs none of them.
    """
    return not unconfigured_detection()


def selectors_ready() -> bool:
    return not unconfigured()


# ── is this even the real page? ─────────────────────────────────────────────
# Amazon fronts the site with CloudFront + a WAF. When it blocks you, it serves
# a normal 200-looking HTML page. Without these checks the scraper would find
# zero job cards and cheerfully report "no shifts available" forever, which is
# the worst possible failure: silent and indistinguishable from a quiet day.
BLOCKED_MARKERS = (
    "403 ERROR",
    "Request blocked",
    "The request could not be satisfied",
    "Generated by cloudfront",
)

# The site's access token is short-lived. This one is NOT fatal: a reload
# refreshes it, which is verified behaviour.
TOKEN_EXPIRED_MARKERS = (
    "Token expired",
    "Problem loading page",
)


# The site ships a CAPTCHA modal (confirmed live: data-test-id="captchaModal").
# It sits in the DOM hidden, so presence alone means nothing — only visibility
# counts. A visible CAPTCHA needs a human, and must never be mistaken for
# "no shifts today".
CAPTCHA_SELECTOR = "[data-test-id='captchaModal']"


# The apply flow is gated by a separate login from ordinary browsing: job
# search is public, so detection works perfectly while the account is signed
# out, and the first sign of trouble is a login tab appearing mid-hold.
# Confirmed live: clicking Apply opened auth.hiring.amazon.com/#/login.
LOGIN_HOSTS = ("auth.hiring.amazon.", "/ap/signin", "/#/login")


def is_login_page(page: Any) -> bool:
    url = (page.url or "").lower()
    return any(marker in url for marker in LOGIN_HOSTS)


def _wait_for_new_page(context: Any, known: set, timeout_ms: int = 8000) -> Any | None:
    """Return a tab that appeared since `known` was captured, or None.

    "Apply" opens the application in a new tab. Without following it the hold
    would keep clicking at the old page and time out with a misleading error
    about a missing button.

    Only ever called for a step declared `opens_popup`. Calling it after a
    step that opens nothing costs the entire timeout for no reason — which is
    exactly the bug this signature comment now exists to prevent.
    """
    if context is None:
        return None
    waited = 0
    while waited < timeout_ms:
        for candidate in context.pages:
            if candidate not in known:
                try:
                    candidate.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
                except Exception as exc:  # noqa: BLE001 - a slow tab is still a tab
                    log.debug("new tab did not settle: %s", exc)
                return candidate
        context.pages[0].wait_for_timeout(100)
        waited += 100
    return None



def _settle_after_popup(page: Any, timeout_ms: int = 20000) -> bool:
    """Wait for the tab Apply opened to become the application.

    It opens as about:blank, redirects through /application/?…&page=pre-consent
    and only then mounts the React app. Clicking into it before that races the
    mount — which is exactly what happened once the accidental 8-second waits
    were removed: the Next button was not there yet and the hold failed 10s
    later having done nothing.

    Readiness, not a fixed sleep: this returns the moment the app is up, which
    was 2.3s in the fast case and is allowed considerably longer in the slow
    one, because a slow tab still holds a shift and a bailout holds nothing.
    """
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception as exc:  # noqa: BLE001 - a slow tab is still a tab
        log.debug("popup did not reach domcontentloaded: %s", exc)

    waited = 0
    while waited < timeout_ms:
        url = page.url or ""
        # A signed-out session lands here instead of the application. Waiting
        # the full timeout for an app that is never coming just delays the
        # message telling you to log in.
        if is_login_page(page):
            log.warning("the application tab went to the login page")
            return False
        if APPLICATION_URL_MARKER in url and url != "about:blank":
            try:
                if page.locator(APPLICATION_ANY_ACTION).first.is_visible():
                    return True
            except Exception as exc:  # noqa: BLE001 - still mounting
                log.debug("application not ready yet: %s", exc)
        try:
            page.wait_for_timeout(100)
        except Exception:  # noqa: BLE001
            return False
        waited += 100

    log.warning("application tab never settled within %dms (at %s)", timeout_ms, page.url)
    return False

def page_state(page: Any) -> tuple[str, str]:
    """Classify the current page.

    Returns (state, detail) where state is one of:
      ok       — a real, usable page
      blocked  — CloudFront/WAF block page
      captcha  — a CAPTCHA is on screen, a human is needed
      stale    — access token expired; a reload fixes this
      login    — session gone, re-run save_session.py
    """
    url = (page.url or "").lower()
    if any(marker in url for marker in ("login", "signin", "sign-in", "/ap/")):
        return "login", f"redirected to {page.url}"

    try:
        captcha = page.locator(CAPTCHA_SELECTOR)
        if captcha.count() > 0 and captcha.first.is_visible():
            return "captcha", "captcha modal is on screen"
    except Exception as exc:  # noqa: BLE001 - absence of the node is normal
        log.debug("captcha probe failed: %s", exc)

    try:
        text = page.inner_text("body") or ""
    except Exception as exc:  # noqa: BLE001 - no body yet is not conclusive
        log.debug("could not read body text: %s", exc)
        return "ok", ""

    for marker in BLOCKED_MARKERS:
        if marker.lower() in text.lower():
            return "blocked", marker

    for marker in TOKEN_EXPIRED_MARKERS:
        if marker.lower() in text.lower():
            return "stale", marker

    return "ok", ""


# ── reading the page ────────────────────────────────────────────────────────
def _text(card, selector: str) -> str:
    """Text of the first match inside a card, or '' if absent/unconfigured."""
    if not selector or selector == TODO:
        return ""
    try:
        node = card.locator(selector).first
        if node.count() == 0:
            return ""
        return (node.inner_text(timeout=2000) or "").strip()
    except Exception as exc:  # noqa: BLE001 - a missing field is not fatal
        log.debug("could not read %s: %s", selector, exc)
        return ""


def on_detail_page(page: Any) -> bool:
    """Are we already looking at a single job, rather than the results list?"""
    if "jobdetail" in (page.url or "").lower():
        return True
    try:
        return page.locator(DETAIL_PAGE_MARKER).count() > 0
    except Exception:  # noqa: BLE001
        return False


def _card_location(card) -> str:
    """Location of a job card.

    Amazon gives the location no data-test-id and no stable class — the only
    thing identifying it is position: it is the last line of the card's text,
    after the "Pay rate:" line. Parsing text is ugly, but it is markedly more
    durable here than a positional CSS path through Emotion-hashed divs.
    """
    selector = SELECTORS.get("card_location")
    if selector and selector != TODO:
        text = _text(card, selector)
        if text:
            return text

    try:
        lines = [
            line.strip()
            for line in (card.inner_text(timeout=2000) or "").splitlines()
            if line.strip()
        ]
    except Exception as exc:  # noqa: BLE001
        log.debug("could not read card text for location: %s", exc)
        return ""
    return lines[-1] if lines else ""


def extract_shifts(page: Any) -> list[Shift]:
    """Scrape the currently rendered results list into Shift objects."""
    if SELECTORS["job_card"] == TODO:
        raise RuntimeError(
            "site_selectors.SELECTORS['job_card'] is still TODO — "
            "fill in the real selectors before running in dom mode"
        )

    cards = page.locator(SELECTORS["job_card"])
    count = cards.count()
    if count == 0:
        no_results = SELECTORS.get("no_results")
        if no_results and no_results != TODO and page.locator(no_results).count() > 0:
            log.debug("site reports no results")
        else:
            # Genuinely ambiguous: either the page is empty or our selector
            # rotted. Say so rather than reporting a confident zero.
            log.warning(
                "0 job cards matched %r and no 'no results' marker was found — "
                "selectors may be stale",
                SELECTORS["job_card"],
            )
        return []

    shifts: list[Shift] = []
    for index in range(count):
        card = cards.nth(index)
        try:
            shift_id = None
            id_attr = SELECTORS.get("card_id_attr")
            if id_attr:
                shift_id = card.get_attribute(id_attr, timeout=2000)

            url = None
            link_sel = SELECTORS.get("card_link")
            if link_sel and link_sel != TODO:
                link = card.locator(link_sel).first
                if link.count() > 0:
                    href = link.get_attribute("href", timeout=2000)
                    # hrefs on the page are usually relative ("/job/123").
                    # page.goto() needs an absolute URL.
                    url = urljoin(page.url, href) if href else None

            shifts.append(
                Shift(
                    id=shift_id,
                    title=_text(card, SELECTORS["card_title"]),
                    location=_card_location(card),
                    schedule=_text(card, SELECTORS["card_schedule"]),
                    pay_rate=_text(card, SELECTORS["card_pay"]),
                    url=url,
                )
            )
        except Exception as exc:  # noqa: BLE001 - skip one bad card, keep the rest
            log.warning("skipping card %d: %s", index, exc)
    return shifts


def find_matching_card(page: Any, shift: Shift):
    """Locate the card for THIS shift.

    Written after a bug where the code grabbed the first card on the page
    regardless of which one matched — which would happily hold the wrong shift.
    Match by the site's id when we have one; otherwise fall back to an exact
    title + location text match.
    """
    cards = page.locator(SELECTORS["job_card"])
    count = cards.count()

    id_attr = SELECTORS.get("card_id_attr")
    if shift.id and id_attr:
        for index in range(count):
            card = cards.nth(index)
            try:
                if (card.get_attribute(id_attr, timeout=2000) or "").strip() == shift.id:
                    return card
            except Exception:  # noqa: BLE001
                continue

    for index in range(count):
        card = cards.nth(index)
        title = _text(card, SELECTORS["card_title"])
        location = _card_location(card)
        if title == shift.title and (not shift.location or location == shift.location):
            return card

    return None


# ── overlays that block clicking ────────────────────────────────────────────
# Confirmed live: a cookie consent modal renders with a full-page backdrop
# (StencilModalBackdrop) that intercepts every pointer event. Reading the page
# still works — which is why detection was fine — but every click in the hold
# flow fails with "subtree intercepts pointer events" until it is dismissed.
#
# Order matters: consent first, since its backdrop covers everything else.
MODAL_BACKDROP = "[data-test-component='StencilModalBackdrop']"

OVERLAY_DISMISSERS: list[tuple[str, str]] = [
    ("cookie consent", "[data-test-id='consentBtn']"),
    ("banner", "[data-test-component='MessageBannerDismissButton']"),
    ("guided search", "[aria-label='Close guided search']"),
]


def _backdrop_visible(page: Any) -> bool:
    try:
        backdrop = page.locator(MODAL_BACKDROP)
        return backdrop.count() > 0 and backdrop.first.is_visible()
    except Exception:  # noqa: BLE001
        return False


def dismiss_overlays(page: Any, timeout_ms: int = 3000, rounds: int = 4) -> list[str]:
    """Close anything covering the page. Returns what was dismissed.

    Dismissing one modal can reveal another — the cookie consent modal sits in
    front of a job-alert signup modal, which appears only sometimes. So this
    loops until the backdrop is actually gone rather than making one pass, and
    falls back to Escape for modals we have no named button for. That keeps it
    working when Amazon adds a new one.

    Deliberately NOT called during detection: dry runs stay genuinely
    click-free, and extraction works fine with a modal up. Only the hold flow
    needs this.

    Consenting once in save_session.py's visible browser also persists in the
    profile, so in practice this is a safety net rather than the main fix.
    """
    dismissed: list[str] = []

    # Fast path. In the common case nothing is covering the page — the consent
    # was accepted once in save_session.py and persists in the profile — and
    # the hold is the one place where a wasted half-second is a lost shift.
    try:
        if not _backdrop_visible(page):
            if not any(
                page.locator(selector).first.count()
                and page.locator(selector).first.is_visible()
                for _label, selector in OVERLAY_DISMISSERS
            ):
                return dismissed
    except Exception as exc:  # noqa: BLE001 - fall through to the full sweep
        log.debug("overlay fast path failed: %s", exc)

    for _ in range(rounds):
        acted = False
        for label, selector in OVERLAY_DISMISSERS:
            try:
                item = page.locator(selector).first
                if item.count() == 0 or not item.is_visible():
                    continue
                item.click(timeout=timeout_ms)
                dismissed.append(label)
                acted = True
                page.wait_for_timeout(250)
            except Exception as exc:  # noqa: BLE001 - absence is the normal case
                log.debug("could not dismiss %s: %s", label, exc)

        if not _backdrop_visible(page):
            break

        if not acted:
            # Something is covering the page that we have no button for.
            try:
                page.keyboard.press("Escape")
                dismissed.append("escape")
                page.wait_for_timeout(250)
            except Exception as exc:  # noqa: BLE001
                log.debug("escape failed: %s", exc)
            if _backdrop_visible(page):
                log.warning("a modal is still covering the page after Escape")
                break

    if dismissed:
        log.info("dismissed overlays: %s", ", ".join(dismissed))
    return dismissed


# ── acting on the page ──────────────────────────────────────────────────────
def _screenshot(page: Any, path: str | None) -> None:
    if not path:
        return
    try:
        page.screenshot(path=path, full_page=False)
    except Exception as exc:  # noqa: BLE001 - an image is never worth failing over
        log.warning("screenshot failed: %s", exc)


def _hold_confirmation(page: Any, timeout_ms: int = 10000) -> str:
    """The site's own "we are holding a spot…" banner, or ''.

    Polled rather than read once: the banner appears after the application is
    created server-side, which takes a moment.
    """
    waited = 0
    while waited < timeout_ms:
        try:
            found = page.evaluate(
                """(pattern) => {
                    const text = document.body ? document.body.innerText : '';
                    const match = text.match(new RegExp(pattern, 'i'));
                    return match ? match[0].trim() : '';
                }""",
                HOLD_CONFIRMATION_PATTERN,
            )
            if found:
                return found
        except Exception as exc:  # noqa: BLE001 - page may still be navigating
            log.debug("could not read the holding banner: %s", exc)
        page.wait_for_timeout(250)
        waited += 250
    return ""


CONFIRMED = "confirmed"
FAILED = "failed"
UNCERTAIN = "uncertain"


class HoldResult:
    """What happened, in three states rather than two.

    The middle state is the point. "Pressed Create Application but never saw
    the holding banner" is neither success nor failure: the application may
    well exist. Calling it held would send you to bed believing you have a
    shift you do not; calling it failed would have you fight for one you
    already hold. It gets its own status, and a message telling you to look.
    """

    def __init__(self, status, message, *, url="", banner="", timings=None):
        self.status = status
        self.message = message
        self.url = url
        self.banner = banner
        self.timings = timings or []

    @property
    def held(self):
        return self.status == CONFIRMED

    # Failures that another schedule might survive, versus ones where trying
    # again is pointless or harmful.
    NOT_WORTH_RETRYING = (
        "needs a login",          # session is dead; every schedule will fail
        "not configured",         # selectors are placeholders
        "no schedule left",       # already exhausted the flyout
        "could not find the card",
    )

    def worth_retrying(self) -> bool:
        """Would the next schedule plausibly do better?

        A sniped slot is worth another attempt; a dead session is not — it
        would fail identically three times while the posting disappears.
        """
        if self.status != FAILED:
            return False
        low = (self.message or "").lower()
        return not any(marker in low for marker in self.NOT_WORTH_RETRYING)

    @property
    def needs_you(self):
        """Should this interrupt the human right now?"""
        return self.status in (FAILED, UNCERTAIN)

    def timing_summary(self):
        return ", ".join("{} {:.0f}ms".format(label, ms) for label, ms in self.timings)

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<HoldResult {}: {}>".format(self.status, self.message[:60])


def hold_at_application(
    page: Any,
    application_url: str,
    *,
    stop_before_submit: bool = True,
    timeout_ms: int = 20000,
    screenshot_path: str | None = None,
) -> HoldResult:
    """Hold a slot by going straight to its application page.

    The click path — card, detail page, schedule flyout, Apply, follow the new
    tab — exists because that is how a human gets there. With a scheduleId in
    hand none of it is necessary: Apply merely navigates to this URL, so we can
    navigate to it ourselves.

    Five page loads become one, and the two most fragile selectors in the
    project (the flyout, and following the popup) stop being on the critical
    path at all.
    """
    # TEMP DIAGNOSTIC: log every POST request during this hold.
    # Remove after capturing the Create Application mutation.
    def _log_hold_request(request):
        if request.method == "POST":
            log.info(
                "HOLD POST: url=%s\nheaders=%s\nbody=%s",
                request.url,
                dict(request.headers),
                request.post_data,
            )

    page.on("request", _log_hold_request)

    began = time.perf_counter()
    timings: list[tuple[str, float]] = []

    def mark(label: str) -> None:
        timings.append((label, (time.perf_counter() - began) * 1000))

    try:
        page.goto(application_url, wait_until="domcontentloaded")
        mark("application opened")
    except Exception as exc:  # noqa: BLE001
        return HoldResult(FAILED, f"could not open the application: {str(exc)[:150]}",
                          url=application_url, timings=timings)

    if is_login_page(page):
        return HoldResult(
            FAILED,
            f"the application needs a login (opened {page.url[:70]}). "
            "Detection works signed out, holding does not — "
            "run `python save_session.py`.",
            url=page.url, timings=timings,
        )

    settled = _settle_after_popup(page, timeout_ms=max(timeout_ms, 20000))

    # Re-check AFTER settling. The redirect to login happens in JavaScript a
    # moment after the navigation resolves, so the check before it can see the
    # application URL and believe everything is fine. Without this the run
    # ends with "could not find the Create Application button", which sends
    # you hunting for a selector change when the truth is you are signed out.
    if is_login_page(page):
        return HoldResult(
            FAILED,
            f"the application redirected to the login page ({page.url[:70]}). "
            "The session is not signed in — run `python save_session.py`.",
            url=page.url, timings=timings,
        )

    if not settled:
        # Not fatal on its own; the button wait below is the real test.
        log.warning("the application page did not settle cleanly")
    mark("application ready")

    dismiss_overlays(page, timeout_ms=min(timeout_ms, 3000))

    # The pre-consent page's only action. Harmless — it commits nothing.
    try:
        page.locator(PRE_CONSENT_NEXT).first.wait_for(state="visible", timeout=timeout_ms)
        page.locator(PRE_CONSENT_NEXT).first.click(timeout=timeout_ms)
        mark("consent screen opened")
    except Exception as exc:  # noqa: BLE001 - some flows land past pre-consent
        log.info("no pre-consent step to click (%s)", str(exc)[:80])

    return _finish_application(
        page,
        stop_before_submit=stop_before_submit,
        timeout_ms=timeout_ms,
        screenshot_path=screenshot_path,
        timings=timings,
        mark=mark,
    )



def schedule_card_texts(page: Any) -> list[str]:
    """Visible text of each schedule card in the flyout, in render order.

    One entry per Apply button, so the index lines up with what a click on
    that Apply would take.
    """
    try:
        return page.evaluate(
            """(flyoutSel) => {
                const flyout = document.querySelector(flyoutSel);
                if (!flyout) return [];
                const applies = flyout.querySelectorAll(
                    "[data-test-id='ScheduleCardSelectScheduleLink']");
                const out = [];
                for (const apply of applies) {
                    let node = apply;
                    for (let i = 0; i < 8 && node.parentElement; i++) {
                        node = node.parentElement;
                        if ((node.innerText || '').length > 40) break;
                    }
                    out.push((node.innerText || '').trim());
                }
                return out;
            }""",
            SCHEDULE_FLYOUT,
        ) or []
    except Exception as exc:  # noqa: BLE001 - never fatal, we fall back to first
        log.debug("could not read the schedule cards: %s", exc)
        return []


def hold_shift(
    page: Any,
    shift: Shift,
    *,
    stop_before_submit: bool = True,
    timeout_ms: int = 10000,
    screenshot_path: str | None = None,
    schedule_index: int = 0,
    schedule_prefs: dict | None = None,
) -> HoldResult:
    """Click through to hold the slot.

    Every step is timed, because the gap between spotting a shift and having
    it reserved is the number this whole program exists to shrink.
    """
    if not selectors_ready():
        return HoldResult(FAILED, f"selectors not configured: {', '.join(unconfigured())}")

    began = time.perf_counter()
    timings: list[tuple[str, float]] = []

    def mark(label: str) -> None:
        timings.append((label, (time.perf_counter() - began) * 1000))

    # Must happen before any click: the consent modal's backdrop swallows
    # pointer events, and every step below would time out.
    dismiss_overlays(page, timeout_ms=min(timeout_ms, 3000))

    mark("overlays dismissed")
    steps = list(HOLD_STEPS)

    if on_detail_page(page):
        # api mode navigates straight to the job URL. There are no cards on
        # that page, so the card-click step is not just unnecessary — trying
        # it would fail.
        log.info("already on the job detail page — skipping the card click")
        scope = page
        steps = steps[1:]
    else:
        card = find_matching_card(page, shift)
        if card is None:
            return HoldResult(
                FAILED, f"could not find the card for {shift.summary()!r} on the page",
                url=getattr(page, "url", "") or "", timings=timings,
            )
        try:
            card.scroll_into_view_if_needed(timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001 - non-fatal
            log.debug("scroll_into_view failed: %s", exc)
        # The first step is scoped to the matched card; later steps run on
        # whatever page/modal the flow navigated to.
        scope = card

    # "pick a shift" must be scoped to the schedule flyout: an unscoped Apply
    # selector could match a button elsewhere on a busy page.
    steps = [
        step._replace(selector=f"{SCHEDULE_FLYOUT} {step.selector}")
        if step.label == "pick a shift" and not step.selector.startswith(SCHEDULE_FLYOUT)
        else step
        for step in steps
    ]

    context = getattr(page, "context", None)

    for step_number, step in enumerate(steps):
        # Snapshot before the click, never after: the tab can exist before
        # control comes back to us, and a listener armed afterwards misses it.
        known_pages = set(context.pages) if (context and step.opens_popup) else set()
        step_started = time.perf_counter()
        try:
            if step.label == "pick a shift":
                # Which Apply button, not just the first one. A job can offer
                # several schedules and the render order is an accident — see
                # schedules.rank_cards. schedule_index selects among them, so a
                # caller can retry with the next one when a slot is sniped.
                texts = schedule_card_texts(page)
                order = schedules.rank_cards(
                    [schedules.parse_card_text(text) for text in texts], schedule_prefs
                ) if texts else []
                if order:
                    if schedule_index >= len(order):
                        return HoldResult(
                            FAILED,
                            f"no schedule left to try: {len(order)} acceptable of "
                            f"{len(texts)} on offer, already tried {schedule_index}",
                            url=getattr(page, "url", "") or "", timings=timings,
                        )
                    wanted = order[schedule_index]
                    log.info(
                        "taking %s",
                        schedules.describe_card(
                            schedules.parse_card_text(texts[wanted]), wanted, len(texts)
                        ),
                    )
                    target = scope.locator(step.selector).nth(wanted)
                else:
                    target = scope.locator(step.selector).first
            else:
                target = scope.locator(step.selector).first
            target.wait_for(state="visible", timeout=timeout_ms)
            target.click(timeout=timeout_ms)
            log.info(
                "hold step %d/%d ok: %s (%.0fms)",
                step_number + 1, len(steps), step.label,
                (time.perf_counter() - step_started) * 1000,
            )
            mark(step.label)
        except Exception as exc:  # noqa: BLE001 - report which step died
            return HoldResult(
                FAILED, f"hold failed at step {step_number + 1} ({step.label}): {exc}",
                url=getattr(page, "url", "") or "", timings=timings,
            )

        # Only Apply opens a tab. Waiting after the others cost the full
        # timeout each and bought nothing — 16 of the 28 seconds on a real
        # hold. Everything after Apply runs in the tab it opened.
        if step.opens_popup:
            popup = _wait_for_new_page(context, known_pages, timeout_ms=min(timeout_ms, 8000))
            if popup is not None:
                log.info("step %r opened a new tab: %s", step.label, popup.url[:80])
                page = popup
                mark("new tab")
                # The tab exists long before the application does. Wait for the
                # app itself, generously — a slow tab still holds a shift.
                if _settle_after_popup(page, timeout_ms=max(timeout_ms, 20000)):
                    mark("application ready")
            else:
                # Not fatal: the site could start navigating in place instead,
                # and the checks below still establish where we ended up.
                log.warning("step %r was expected to open a tab and did not", step.label)

        # A login tab here means the hiring portal is signed out. Job search is
        # public, so detection kept working and nothing warned us until now.
        # Say so plainly instead of timing out on a button that will never come.
        if is_login_page(page):
            return HoldResult(
                FAILED,
                f"the apply flow needs a login (opened {page.url[:70]}). "
                "Detection works signed out, holding does not — "
                "run `python save_session.py` and log in to the hiring portal.",
                url=page.url, timings=timings,
            )

        scope = page  # subsequent steps are page-wide

    # Wait for the consent screen to actually render BEFORE capturing it. The
    # tab opens blank and the app mounts a second or two later, so
    # screenshotting immediately produced a plain white image — a Telegram
    # alert carrying a blank photo reads as a failure even when the hold worked.
    return _finish_application(
        page,
        stop_before_submit=stop_before_submit,
        timeout_ms=timeout_ms,
        screenshot_path=screenshot_path,
        timings=timings,
        mark=mark,
    )


def _finish_application(
    page: Any,
    *,
    stop_before_submit: bool,
    timeout_ms: int,
    screenshot_path: str | None,
    timings: list,
    mark,
) -> HoldResult:
    """The last stretch, shared by both routes into the application.

    Whether we clicked our way here or navigated straight to the URL, the
    decisions from the consent screen onwards are identical — and so are the
    ways it can go wrong.
    """
    ready_error = None
    try:
        # Readiness, not a fixed sleep: this returns the instant the button
        # renders, which also proves the consent screen actually mounted.
        page.locator(CREATE_APPLICATION).first.wait_for(state="visible", timeout=timeout_ms)
        mark("consent screen ready")
    except Exception as exc:  # noqa: BLE001 - the URL check below still applies
        ready_error = exc

    landed = APPLICATION_URL_MARKER in (page.url or "")
    url = page.url or ""

    if stop_before_submit:
        _screenshot(page, screenshot_path)
        if ready_error is not None:
            if landed:
                return HoldResult(
                    UNCERTAIN,
                    f"application open at {url} — but the Create Application "
                    f"button was not found ({ready_error}). THE SPOT IS NOT HELD.",
                    url=url, timings=timings,
                )
            return HoldResult(
                FAILED,
                f"clicked through but never reached the application "
                f"(still at {url}): {ready_error}",
                url=url, timings=timings,
            )
        # Deliberate: everything so far is reversible browsing. Create
        # Application accepts the drug-test and age declarations and is what
        # reserves the slot, so it is gated behind stop_before_submit: false.
        return HoldResult(
            UNCERTAIN,
            "stopped at the consent screen — NOT held. Finish it yourself at "
            f"{url}, or set hold.stop_before_submit: false to have the "
            "watcher press Create Application for you.",
            url=url, timings=timings,
        )

    if ready_error is not None:
        _screenshot(page, screenshot_path)
        return HoldResult(
            FAILED if not landed else UNCERTAIN,
            f"could not find the Create Application button: {ready_error}",
            url=url, timings=timings,
        )

    try:
        page.locator(CREATE_APPLICATION).first.click(timeout=timeout_ms)
        mark("create application clicked")
    except Exception as exc:  # noqa: BLE001
        _screenshot(page, screenshot_path)
        return HoldResult(
            FAILED, f"Create Application click failed: {exc}", url=url, timings=timings,
        )

    # Never assume the click worked. The site states the hold itself, with a
    # countdown — read it back rather than inferring success from a click that
    # returned without throwing.
    confirmation = _hold_confirmation(page, timeout_ms=timeout_ms)
    if confirmation:
        mark("hold confirmed")
    url = page.url or url
    _screenshot(page, screenshot_path)
    mark("screenshot")

    if confirmation:
        return HoldResult(
            CONFIRMED,
            f"SPOT HELD — {confirmation}\nFinish the steps at {url}",
            url=url, banner=confirmation, timings=timings,
        )
    # Clicked, but unproven. Not held, not failed — look.
    return HoldResult(
        UNCERTAIN,
        "Create Application was clicked but the holding banner never appeared "
        f"— check it by hand at {url}",
        url=url, timings=timings,
    )
