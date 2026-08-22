"""Collect non-secret evidence from the final Hiring page after auth.

This exists to diagnose UNKNOWN_PAGE without weakening authentication rules.
Only structural names/booleans are recorded: no cookie values, storage values,
credentials, challenge parameters, solver tokens, email addresses, or PINs.
"""

from __future__ import annotations

from urllib.parse import urlparse


def _host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower()


def collect(page, context, base_url: str) -> dict:
    """Return a sanitized structural snapshot of the current browser state."""
    expected_host = _host(base_url)
    result = {
        "host": _host(getattr(page, "url", "")),
        "path": "",
        "title": "",
        "visible_test_ids": [],
        "visible_element_ids": [],
        "local_storage_keys": [],
        "session_storage_keys": [],
        "cookie_names": [],
        "login_controls_visible": False,
        "account_text_marker_visible": False,
        "application_action_visible": False,
    }

    try:
        dom = page.evaluate(
            """() => {
                const visible = (el) => {
                    if (!el) return false;
                    const s = getComputedStyle(el);
                    const r = el.getBoundingClientRect();
                    return s.visibility !== 'hidden' && s.display !== 'none' &&
                           r.width > 0 && r.height > 0;
                };
                const uniq = (xs) => Array.from(new Set(xs.filter(Boolean))).slice(0, 120);
                const text = (document.body?.innerText || '').toLowerCase();
                const testIds = uniq(Array.from(document.querySelectorAll('[data-test-id]'))
                    .filter(visible).map(el => el.getAttribute('data-test-id')));
                const ids = uniq(Array.from(document.querySelectorAll('[id]'))
                    .filter(visible).map(el => el.id));
                const loginSelectors = [
                    "[data-test-id='input-test-id-login']",
                    "[data-test-id='input-test-id-confirmOtp'] input",
                    "[data-test-id='input-test-id-pin']",
                    "#country-toggle-button"
                ];
                const appSelectors = [
                    "[data-test-id='text-pre-consent-page-title']",
                    "[data-test-id='layout'] button"
                ];
                return {
                    path: location.pathname + location.search + location.hash,
                    title: document.title || '',
                    visible_test_ids: testIds,
                    visible_element_ids: ids,
                    local_storage_keys: Object.keys(localStorage).sort().slice(0, 120),
                    session_storage_keys: Object.keys(sessionStorage).sort().slice(0, 120),
                    login_controls_visible: loginSelectors.some(sel => {
                        try { return visible(document.querySelector(sel)); } catch (_) { return false; }
                    }),
                    account_text_marker_visible: [
                        'my account', 'sign out', 'welcome back'
                    ].some(marker => text.includes(marker)),
                    application_action_visible: appSelectors.some(sel => {
                        try { return visible(document.querySelector(sel)); } catch (_) { return false; }
                    })
                };
            }"""
        )
        if isinstance(dom, dict):
            for key in (
                "path", "title", "visible_test_ids", "visible_element_ids",
                "local_storage_keys", "session_storage_keys",
                "login_controls_visible", "account_text_marker_visible",
                "application_action_visible",
            ):
                if key in dom:
                    result[key] = dom[key]
    except Exception:
        pass

    try:
        cookies = context.cookies([base_url]) if context is not None else []
        names = []
        for cookie in cookies or []:
            domain = str(cookie.get("domain") or "").lstrip(".").lower()
            if expected_host and (domain == expected_host or expected_host.endswith("." + domain)):
                name = str(cookie.get("name") or "")
                if name:
                    names.append(name)
        result["cookie_names"] = sorted(set(names))[:120]
    except Exception:
        pass

    return result
