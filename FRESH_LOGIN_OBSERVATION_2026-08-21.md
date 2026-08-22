# Fresh Canada login observation — 2026-08-21

This note records a live, user-authorized browser observation. It intentionally
contains no email address, PIN, OTP, cookies, tokens, authorization headers,
CAPTCHA solution, or KYC tracking identifier.

## Confirmed state sequence

1. Open `https://auth.hiring.amazon.com/#/login`.
2. Email -> Continue.
3. Personal PIN -> Continue.
4. Verification destination -> Send verification code.
5. An AWS WAF visual challenge can appear without a useful auth URL change.
6. After the challenge is confirmed, Amazon advances to the OTP screen itself.
7. Fill the six-digit OTP -> Verify.
8. Wait for Amazon's accepted state and its enabled Continue button.
9. Click Continue exactly once.
10. Tolerate a brief auth return and a landing at `https://hiring.amazon.ca/`.
11. Reopen the exact Canada application route when the auth return does not
    preserve the application redirect.
12. Require the separate protected-candidate 2xx proof before production code
    marks the hold session ready or re-armed.

Auth steps must be classified from visible DOM state, not URL transitions.
The country selector was absent; only the language selector was present, so a
country selector must remain optional and non-blocking.

## Safe live timings

Pre-challenge observation from a continuous fresh-login attempt:

- initial navigation return: 6,694 ms
- email visible: 9,672 ms
- email submitted: 15,554 ms
- PIN visible: 16,042 ms
- PIN submitted: 18,560 ms
- verification destination visible: 19,707 ms
- verification code requested: 22,579 ms
- visual challenge observed: 23,787 ms

Post-challenge observation after the user completed the visual challenge and
entered the OTP directly in the browser:

- Verify click -> accepted Continue visible: 1,126 ms
- Continue click action: 1,762 ms
- auth return landed at the Canada hiring homepage
- exact application-route navigation returned: 402 ms
- protected Create Application visible and enabled: 2,421 ms from navigation

The visible and enabled Create Application control confirms that the browser
returned to a protected candidate UI. It does not replace the required backend
2xx session proof used by the watcher.

## Implementation implications

- Start mailbox waiting when Amazon sends the verification code, including
  while a challenge is being solved.
- Do not keep interacting with destination/send-code controls after the DOM has
  transitioned to the OTP screen.
- Do not click Verify and immediately leave. Wait for the accepted/green state
  and enabled Continue button, then click Continue once.
- Treat a short return to the auth origin or the Canada hiring homepage as an
  intermediate success state, then prove the protected session strictly.
- The direct application route still requires normal Create Application and
  Integrity Notice/I Agree actions. This observation does not justify private
  reservation replay or bypassing required UI steps.
- Browser timings are observations, not a reservation-success claim or a
  production performance guarantee.

## Live availability / ghost-state evidence

Observed after the fresh authenticated login on the same date:

- Recommended jobs: zero cards.
- All jobs: zero cards.
- Re-querying the public All-jobs state for five minutes (93 cycles) produced
  zero public job cards. No application attempt was made from that empty state.
- The previously detected Barrhaven job/schedule still had a directly
  addressable job-detail page, but the page explicitly displayed
  `This job is not available for application now.`
- Despite that terminal warning, Amazon still rendered a normal-looking Select
  schedule button. A normal click succeeded in under one second and opened a
  real schedule panel whose authoritative contents were `0 schedules found`
  and `there are no schedules that match your filter choices`.

Therefore button presence, visibility, enabled state, and click success do not
prove schedule capacity. A valid candidate for the live reservation trace must
have all of the following immediately before application dispatch:

1. a job card currently returned by Amazon's public All-jobs state;
2. a schedule card with the exact schedule ID and non-zero/unknown bookable
   capacity from Amazon's schedule response;
3. no explicit unposted-job warning; and
4. no explicit zero-schedule panel.

Even those checks cannot guarantee a reserve because capacity can be consumed
between catalog observation and Amazon's application update. Final success
still requires `JOB_SELECTED`, the exact schedule ID, and a soft-reserve expiry
from Amazon's own observed application response (or an equivalent explicit
holding banner). The 2026-08-21 observation did not produce a valid public
schedule, so it is not a live hold-success claim.

## Polling-transport evidence

The public React search UI was deliberately re-queried in-browser to test
whether it could serve as a low-latency availability monitor. After 93 All-tab
cycles over five minutes, Amazon first rendered `Problem loading page. The
server didn't respond in time.` A single reload after a 20-second backoff then
rendered Amazon's `Let's confirm you are human` security check.

Therefore rapid UI tab-cycling/reloading is not a valid watcher transport. It
creates much more frontend/WAF work than one catalog request, and an empty card
count during either the server-timeout or security-check state must not be
classified as an empty catalog.

A separate repository API detector was then started with holding disabled,
Telegram disabled, broad Canada matching, and both normal/hot intervals set to
the previously measured-clean 2.0 seconds. Its first 124 polls were clean, each
returned zero cards in roughly 101-303 ms, and produced no 403/429 response.
This is evidence for keeping detection on the catalog API with its circuit
breaker, not for using browser reloads as a faster substitute. The security
challenge itself was left for the applicant; no CAPTCHA-solving behavior was
changed or invoked by this research.

## Saved-route application trace (ghost schedule)

The previously saved Barrhaven application URL was inspected again in an
authenticated browser. This remained a workflow-mapping exercise only: the
same job/schedule had already been proven unavailable and cannot establish a
successful hold.

- The consent route showed an enabled `Create Application` button.
- A normal browser click was used (no forced or JavaScript click).
- The visible/enabled readiness checks completed in 72 ms and the click action
  completed in 368 ms.
- The application-integrity route then showed an enabled `I Agree` button.
- The live notice explicitly stated that clicking `I Agree` certifies that the
  applicant alone is completing the application and is not using a bot, AI
  tool, service, or third party. Automated interaction was stopped at that
  attestation.
- During the applicant's subsequent interaction, the tab navigated to
  `https://auth.hiring.amazon.com/#/login`.
- The only new application-side diagnostic was a browser console error reporting
  HTTP 410 from an application frontend request. No endpoint, request payload,
  cookie, token, authorization header, or identifier was recorded.

The safe classification is therefore **application continuation rejected or
gone**, not identity verification required and not reserved. The evidence does
not distinguish an expired candidate application from another server-side 410
condition, and it does not justify treating every auth redirect as a 401. The
production design remains correct to require a protected-candidate 2xx proof
before arming holds and to classify 401 separately from missing/403/network or
other inconclusive failures.

## Identity-verification launcher state

The applicant subsequently opened a saved Canada application at the
`#/liveness-check` route. Read-only DOM inspection confirmed:

- heading: `Let's confirm it's you`;
- two identity-consent checkboxes were present and both were already checked by
  the applicant;
- `Start identity verification` was visible and enabled;
- the launcher text still described a video-selfie liveness check and government
  ID upload even though the account may have completed eKYC previously; and
- no reserve-success, schedule-unavailable, or holding confirmation was visible
  on this launcher page.

Therefore the presence of `#/liveness-check`, both checked consents, and an
enabled Start button proves only that the launcher is ready. It does **not**
prove that remote KYC will be required again, skipped, or that the schedule is
reserved. That classification can be made only from the safe page/application
state observed after the applicant presses Start. No consent control or Start
button was operated by the observer.

After the applicant pressed Start, the browser did **not** navigate to the
`amazon.in/remoteKYC` service. It returned to the Canada
`#/application-integrity-notice` route, where `I Agree` was visible again. The
application frontend logged an HTTP 400 error during this transition. This is
consistent with remote KYC not being launched, but the saved application is old
and the 400 response prevents concluding that prior eKYC completion was the
reason. There was still no reserve confirmation. A currently available
job/schedule is required to separate an eKYC skip from stale-application
rejection.

## Coordinated unavailable-result trace

In a second applicant-operated trace on the saved unavailable schedule:

- `Create Application` was visible and enabled on `#/consent`;
- after the applicant clicked it, `I Agree` was already visible and enabled at
  the first 19 ms observation on `#/application-integrity-notice`;
- after the applicant clicked `I Agree`, Amazon routed to
  `#/no-available-shift` and displayed `At present, all shifts have been filled
  for this job.`;
- the application frontend also logged HTTP 410; and
- no liveness launcher, remote KYC page, holding banner, or reserve proof was
  present.

The 18.6-second observation loop initially missed this terminal state because
its text matcher did not include Amazon's exact all-shifts-filled wording and
its route matcher did not include `#/no-available-shift`. The classifier now
recognizes either signal immediately. This eliminates needless waiting for this
explicit result without weakening reserve confirmation.
