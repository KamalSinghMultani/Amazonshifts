"""Read Amazon's emailed verification code, so a re-login can finish itself.

WHY THIS EXISTS
---------------
A re-login gets as far as email + PIN unaided, and then Amazon offers to send a
6-digit code by email. Without a way to read that code the automation stops
there and waits for a human — which is exactly the 3am gap this was meant to
close.

WHAT IT GRANTS
--------------
IMAP access to the mailbox, with an app password. That is a bigger grant than
the hiring PIN: it can read every message in the account, not just Amazon's.
It is opt-in for that reason, and it is worth using a dedicated address for
Amazon Hiring if you would rather not hand this to a script.

WHAT IT WILL NOT DO
-------------------
* It never reads mail unless a login attempt is actually in progress.
* It only accepts a code from a message that arrived AFTER the attempt began,
  so a stale code from an hour ago can never be replayed.
* It only looks at messages from Amazon, and never marks anything read or
  deletes anything.
"""

from __future__ import annotations

import email
import imaplib
import logging
import os
import re
import time
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_HOST = "imap.gmail.com"

# Amazon's hiring mail comes from a few different addresses; matching on the
# domain fragment is more durable than a full address that will change.
SENDER_MARKERS = ("amazon", "hiring")

# What the server is asked for. Everything not matching one of these is never
# downloaded at all — the filtering happens on Amazon's side of the wire, not
# in this process.
SEARCH_SENDERS = ("amazon.com", "amazon.ca", "hiring.amazon.com")

# A code that is not among the last few Amazon messages is not the one we just
# asked for, so there is no reason to trawl further back.
MAX_MESSAGES = 10

# The code itself: six digits, standing alone. Bounded so it cannot pick up
# part of a longer number such as a job id or a phone number.
CODE_PATTERN = re.compile(r"(?<!\d)(\d{6})(?!\d)")

# Lines that contain a six-digit number which is NOT the code.
NOT_A_CODE = ("job-", "req", "phone", "©", "amazon.com, inc")


def configured() -> tuple[str, str, str] | None:
    """(host, user, app password) or None if the feature is not set up."""
    user = os.getenv("OTP_IMAP_USER", "").strip()
    password = os.getenv("OTP_IMAP_PASSWORD", "").strip()
    host = os.getenv("OTP_IMAP_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    if not user or not password:
        return None
    return host, user, password


def _decoded(value: Any) -> str:
    try:
        return str(make_header(decode_header(str(value or ""))))
    except Exception:  # noqa: BLE001 - a weird header is not fatal
        return str(value or "")


def body_text(message: Any) -> str:
    """Flatten a message to text, preferring text/plain."""
    if not message.is_multipart():
        try:
            return message.get_payload(decode=True).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return str(message.get_payload())

    chunks = []
    for part in message.walk():
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        try:
            chunks.append(part.get_payload(decode=True).decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            continue
    return "\n".join(chunks)


def extract_code(text: str) -> str | None:
    """Pull the 6-digit code out of a message body.

    Prefers a line that looks like it is presenting a code, because Amazon's
    mail is full of other six-digit numbers — job ids, years, footers.
    """
    if not text:
        return None

    lines = [line.strip() for line in re.split(r"[\n\r]+", text) if line.strip()]
    hinted = [
        line for line in lines
        if any(word in line.lower() for word in ("code", "verification", "otp"))
    ]

    for group in (hinted, lines):
        for line in group:
            low = line.lower()
            if any(bad in low for bad in NOT_A_CODE):
                continue
            match = CODE_PATTERN.search(line)
            if match:
                return match.group(1)

    match = CODE_PATTERN.search(re.sub(r"<[^>]+>", " ", text))
    return match.group(1) if match else None


def is_from_amazon(sender: str) -> bool:
    low = (sender or "").lower()
    return any(marker in low for marker in SENDER_MARKERS)


def message_is_new_enough(message: Any, since_epoch: float) -> bool:
    """Only a code that arrived after the attempt began may be used.

    Without this, a code emailed an hour ago could be replayed into a fresh
    login — which would fail, burn the one attempt allowed, and look like a
    broken PIN.
    """
    raw = message.get("Date")
    if not raw:
        return False
    try:
        return parsedate_to_datetime(raw).timestamp() >= since_epoch - 60
    except Exception:  # noqa: BLE001 - unparseable date is not trustworthy
        return False


def fetch_code(since_epoch: float, *, timeout_s: float = 100, poll_s: float = 5) -> str | None:
    """Wait for Amazon's verification code to arrive, and return it.

    Returns None rather than raising: a failed re-login must leave the watcher
    detecting and alerting exactly as it was.

    The timings are set by Amazon, not chosen: the code expires after THREE
    MINUTES and resend is blocked for 55 seconds, so this polls every 5s and
    gives up at 100s — early enough to leave the code usable, late enough to
    outlast normal mail delay.
    """
    creds = configured()
    if creds is None:
        return None
    host, user, password = creds

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with imaplib.IMAP4_SSL(host) as client:
                client.login(user, password)
                # readonly=True: the server will not set \Seen, so nothing in
                # the mailbox changes just because this looked.
                client.select("INBOX", readonly=True)

                since = time.strftime("%d-%b-%Y", time.localtime(since_epoch - 86400))
                # The sender filter is applied SERVER-SIDE, deliberately. A
                # client-side filter would still download every recent message
                # before discarding it; this way non-Amazon mail is never
                # fetched at all, and the only bodies that ever reach this
                # process are Amazon's own.
                numbers: list[bytes] = []
                for sender in SEARCH_SENDERS:
                    status, data = client.search(None, f'(SINCE {since} FROM "{sender}")')
                    if status == "OK":
                        numbers.extend((data[0] or b"").split())

                # Newest first, and a hard cap: a code that is not in the last
                # few Amazon messages is not the one we just asked for.
                for num in list(dict.fromkeys(reversed(numbers)))[:MAX_MESSAGES]:
                    # BODY.PEEK rather than RFC822: belt and braces against
                    # marking mail read, even though the select is readonly.
                    status, raw = client.fetch(num, "(BODY.PEEK[])")
                    if status != "OK" or not raw or not raw[0]:
                        continue
                    message = email.message_from_bytes(raw[0][1])
                    if not is_from_amazon(_decoded(message.get("From"))):
                        continue  # the server filter is broad; verify anyway
                    if not message_is_new_enough(message, since_epoch):
                        continue
                    code = extract_code(
                        _decoded(message.get("Subject")) + "\n" + body_text(message)
                    )
                    if code:
                        log.info("verification code received by email")
                        return code
        except Exception as exc:  # noqa: BLE001 - mail is best effort
            log.warning("could not read the verification code: %s", exc)

        time.sleep(poll_s)

    log.warning("no verification code arrived within %.0fs", timeout_s)
    return None
