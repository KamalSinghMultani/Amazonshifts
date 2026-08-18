"""Telegram notifications with retry/backoff.

Design rule: notifying is best-effort and must NEVER take down the watcher.
Every public method swallows its exceptions and returns a bool.
"""

from __future__ import annotations

import html
import logging
import os
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"


class TelegramNotifier:
    def __init__(
        self,
        enabled: bool = True,
        token: str | None = None,
        chat_id: str | None = None,
        send_screenshots: bool = True,
        max_retries: int = 4,
        timeout: float = 10.0,
    ) -> None:
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN") or ""
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID") or ""
        self.send_screenshots = send_screenshots
        self.max_retries = max_retries
        self.timeout = timeout

        self.enabled = bool(enabled and self.token and self.chat_id)
        if enabled and not self.enabled:
            log.warning(
                "Telegram notifications requested but TELEGRAM_BOT_TOKEN / "
                "TELEGRAM_CHAT_ID are not set — running without alerts."
            )

    # ── low level ───────────────────────────────────────────────────────────
    def _post(self, method: str, *, data: dict, photo: tuple[str, bytes] | None = None) -> bool:
        """POST with retry/backoff.

        `photo` is (filename, bytes) rather than an open handle — a handle is
        consumed by the first attempt and would upload zero bytes on retry.
        """
        url = f"{API_BASE}/bot{self.token}/{method}"
        delay = 1.0
        for attempt in range(1, self.max_retries + 1):
            files = {"photo": photo} if photo else None
            try:
                resp = requests.post(url, data=data, files=files, timeout=self.timeout)
                if resp.status_code == 200:
                    return True
                # Telegram tells us exactly how long to wait when rate limited.
                if resp.status_code == 429:
                    retry_after = 1.0
                    try:
                        retry_after = float(
                            resp.json().get("parameters", {}).get("retry_after", 1)
                        )
                    except ValueError:
                        pass
                    log.warning("telegram rate limited, sleeping %.1fs", retry_after)
                    time.sleep(retry_after)
                    continue
                log.warning(
                    "telegram %s failed (attempt %d/%d): %s %s",
                    method, attempt, self.max_retries, resp.status_code, resp.text[:200],
                )
            except requests.RequestException as exc:
                log.warning(
                    "telegram %s error (attempt %d/%d): %s",
                    method, attempt, self.max_retries, exc,
                )

            if attempt < self.max_retries:
                time.sleep(delay)
                delay *= 2
        return False

    # ── public ──────────────────────────────────────────────────────────────
    def send_text(self, text: str) -> bool:
        if not self.enabled:
            log.info("[notify muted] %s", text.replace("\n", " | ")[:300])
            return False
        return self._post(
            "sendMessage",
            data={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )

    def send_photo(self, image_path: str | Path, caption: str = "") -> bool:
        if not self.enabled or not self.send_screenshots:
            return False
        path = Path(image_path)
        if not path.exists():
            log.warning("screenshot %s does not exist, skipping photo", path)
            return False
        try:
            return self._post(
                "sendPhoto",
                data={"chat_id": self.chat_id, "caption": caption[:1024], "parse_mode": "HTML"},
                photo=(path.name, path.read_bytes()),
            )
        except OSError as exc:
            log.warning("could not read screenshot %s: %s", path, exc)
            return False

    def describe(self, shift) -> str:
        """One-line HTML summary, shared by every message about a shift."""
        esc = html.escape
        bits = [f"<b>{esc(shift.title or '(untitled)')}</b>"]
        if shift.location:
            bits.append(f"📍 {esc(shift.location)}")
        if shift.schedule:
            bits.append(f"🕒 {esc(shift.schedule)}")
        if shift.pay_rate is not None:
            bits.append(f"💵 ${shift.pay_rate:.2f}/hr")
        return chr(10).join(bits)

    def notify_shift(self, shift, dry_run: bool = True) -> bool:
        """The alert that matters. Sent as early as possible — before any page
        load or click — so the user hears about it at the same moment the bot
        does."""
        esc = html.escape
        lines = [
            "🚨 <b>Shift available</b>",
            f"<b>{esc(shift.title or '(untitled)')}</b>",
        ]
        if shift.location:
            lines.append(f"📍 {esc(shift.location)}")
        if shift.schedule:
            lines.append(f"🕒 {esc(shift.schedule)}")
        if shift.pay_rate is not None:
            lines.append(f"💵 ${shift.pay_rate:.2f}/hr")
        if shift.url:
            lines.append(f'\n<a href="{esc(shift.url)}">Open listing</a>')
        lines.append(
            "\n<i>dry run — no clicks made</i>"
            if dry_run
            else "\n<i>attempting to hold the slot…</i>"
        )
        return self.send_text("\n".join(lines))

    def notify_held(
        self, shift, stopped_before_submit: bool = True, detail: str = ""
    ) -> bool:
        """Report what actually happened to the slot.

        The distinction is the whole message: a spot that is genuinely
        reserved for three hours, versus one the watcher merely walked up to
        and left open for somebody else. Reading "held" and finding the shift
        gone is the worst outcome this tool can produce, so the wording never
        blurs the two.
        """
        if stopped_before_submit:
            head = "⚠️ <b>NOT held — you need to act</b>"
            tail = (
                "The watcher stopped at the consent screen without pressing "
                "<b>Create Application</b>, so this shift is still open to "
                "everyone else. Open it and press that button to hold it.\n\n"
                "To have the watcher do this itself, set "
                "<code>hold.stop_before_submit: false</code>."
            )
        else:
            head = "✅ <b>SPOT HELD</b>"
            tail = (
                "Amazon is holding this for about <b>3 hours</b>. Finish the "
                "remaining steps before it lapses."
            )

        lines = [head, f"<b>{html.escape(shift.title or 'Shift')}</b>"]
        if shift.location:
            lines.append(f"📍 {html.escape(shift.location)}")
        lines.append(tail)
        if detail:
            lines.append(f"\n<pre>{html.escape(detail[:500])}</pre>")
        return self.send_text("\n".join(lines))

    def notify_error(self, message: str) -> bool:
        return self.send_text(f"⚠️ <b>Watcher error</b>\n<pre>{html.escape(message[:600])}</pre>")
