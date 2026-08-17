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
from typing import Any
from urllib.parse import urljoin

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

# The click path to hold a slot, in order. Each step is (label, selector).
# Everything here IS clicked when dry_run is false.
# ":scope" means "the matched card itself" — job cards are role="link" divs
# with a JS click handler, so the card IS the button.
HOLD_STEPS: list[tuple[str, str]] = [
    # CONFIRMED: the card is role="link" — clicking it opens
    #   /app#/jobDetail?jobId=JOB-US-0000018024
    ("open job", ":scope"),
    # CONFIRMED: the detail page's primary action.
    ("select schedule", "[data-test-id='jobDetailSelectScheduleButton']"),
    # NOT captured. Going past "Select schedule" means picking a real shift and
    # starting a REAL application on a real account, so it was left alone.
    # Fill these in against a job you actually want, with dry_run still true.
    ("pick a shift", TODO),      # a row/card in the schedule list
    ("create application", TODO),
]

# Marks a job detail page. Used to tell "we are on the results list" from
# "we already navigated straight to the job", which matters because api mode
# jumps directly to the detail URL and there are no cards there to click.
DETAIL_PAGE_MARKER = "[data-test-id='jobDetailSelectScheduleButton']"

# The detail page DOES expose the job id ([data-test-id='jobDetailInfoJobId'],
# and it is in the URL), even though the search cards do not. So in api mode,
# where real job ids are available, set config api.url_template to
#   https://hiring.amazon.ca/app#/jobDetail?jobId={id}
# and the watcher can jump straight to the job instead of hunting for a card.

# The final button. This is NEVER clicked while hold.stop_before_submit is
# true — we only confirm it is on screen, screenshot it, and hand over to the
# human. Treat this constant as load-bearing safety, not decoration.
FINAL_SUBMIT: str = TODO  # e.g. "button:has-text('Submit application')"


def unconfigured() -> list[str]:
    """Names of every selector still left as a placeholder."""
    missing = [name for name, sel in SELECTORS.items() if sel == TODO]
    missing += [f"HOLD_STEPS[{i}] {label}" for i, (label, sel) in enumerate(HOLD_STEPS) if sel == TODO]
    if FINAL_SUBMIT == TODO:
        missing.append("FINAL_SUBMIT")
    return missing


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
                page.wait_for_timeout(400)
            except Exception as exc:  # noqa: BLE001 - absence is the normal case
                log.debug("could not dismiss %s: %s", label, exc)

        if not _backdrop_visible(page):
            break

        if not acted:
            # Something is covering the page that we have no button for.
            try:
                page.keyboard.press("Escape")
                dismissed.append("escape")
                page.wait_for_timeout(400)
            except Exception as exc:  # noqa: BLE001
                log.debug("escape failed: %s", exc)
            if _backdrop_visible(page):
                log.warning("a modal is still covering the page after Escape")
                break

    if dismissed:
        log.info("dismissed overlays: %s", ", ".join(dismissed))
    return dismissed


# ── acting on the page ──────────────────────────────────────────────────────
def hold_shift(
    page: Any,
    shift: Shift,
    *,
    stop_before_submit: bool = True,
    timeout_ms: int = 10000,
    screenshot_path: str | None = None,
) -> tuple[bool, str]:
    """Click through to hold the slot.

    Returns (ok, message). Stops one step before the final submit unless
    stop_before_submit is explicitly false.
    """
    if not selectors_ready():
        return False, f"selectors not configured: {', '.join(unconfigured())}"

    # Must happen before any click: the consent modal's backdrop swallows
    # pointer events, and every step below would time out.
    dismiss_overlays(page, timeout_ms=min(timeout_ms, 3000))

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
            return False, f"could not find the card for {shift.summary()!r} on the page"
        try:
            card.scroll_into_view_if_needed(timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001 - non-fatal
            log.debug("scroll_into_view failed: %s", exc)
        # The first step is scoped to the matched card; later steps run on
        # whatever page/modal the flow navigated to.
        scope = card

    for step_number, (label, selector) in enumerate(steps):
        try:
            target = scope.locator(selector).first
            target.wait_for(state="visible", timeout=timeout_ms)
            target.click(timeout=timeout_ms)
            log.info("hold step %d/%d ok: %s", step_number + 1, len(steps), label)
        except Exception as exc:  # noqa: BLE001 - report which step died
            return False, f"hold failed at step {step_number + 1} ({label}): {exc}"
        scope = page  # subsequent steps are page-wide

    if screenshot_path:
        try:
            page.screenshot(path=screenshot_path, full_page=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("screenshot failed: %s", exc)

    if stop_before_submit:
        try:
            page.locator(FINAL_SUBMIT).first.wait_for(state="visible", timeout=timeout_ms)
            return True, "slot held — final submit is on screen, waiting for you"
        except Exception as exc:  # noqa: BLE001
            return True, f"steps completed but the submit button was not visible: {exc}"

    try:
        page.locator(FINAL_SUBMIT).first.click(timeout=timeout_ms)
        return True, "application submitted automatically"
    except Exception as exc:  # noqa: BLE001
        return False, f"final submit click failed: {exc}"
