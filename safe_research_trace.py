"""Sanitized, passive browser/network trace for the bounded real hold test.

This intentionally captures the *shape* and timing of Amazon's frontend work,
not a replayable session.  It never reads cookies, storage, request headers, or
authorization values.  Response JSON is retained only for explicitly public
job-catalog operations, after recursive sensitive-field redaction.  Other
GraphQL request bodies are touched only long enough to retain the operation
name and variable *names*.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_PUBLIC_QUERY_VALUES = {
    "country",
    "countrycode",
    "jobid",
    "locale",
    "marketplaceid",
    "page",
    "scheduleid",
}
_SENSITIVE_QUERY_KEYS = {
    "accesstoken",
    "auth",
    "authorization",
    "code",
    "email",
    "idtoken",
    "jwt",
    "otp",
    "password",
    "pin",
    "refreshtoken",
    "signature",
    "token",
    "trackingid",
}
_SENSITIVE_FLOW_MARKERS = ("/liveness-check", "remotekyc")
_SAFE_RESPONSE_HEADERS = {
    "age",
    "cache-control",
    "content-length",
    "content-type",
    "date",
    "etag",
    "last-modified",
    "server-timing",
    "via",
    "x-cache",
}
_GRAPHQL_FIELD = re.compile(
    r"\b(getJobDetail|searchJobCardsByLocation|searchScheduleCards)\b"
)
_PUBLIC_CATALOG_OPERATIONS = {
    "getJobDetail",
    "searchJobCardsByLocation",
    "searchScheduleCards",
}
_SENSITIVE_JSON_KEYS = {
    "applicantid",
    "authorization",
    "birthdate",
    "candidateid",
    "dateofbirth",
    "email",
    "firstname",
    "fullname",
    "governmentid",
    "lastname",
    "legalname",
    "otp",
    "password",
    "personid",
    "phone",
    "pin",
    "sin",
    "ssn",
    "userid",
}
_SENSITIVE_JSON_KEY_MARKERS = (
    "auth",
    "captcha",
    "cookie",
    "credential",
    "document",
    "secret",
    "signature",
    "token",
    "tracking",
)
_MAX_PUBLIC_JSON_BYTES = 2_000_000


def _bounded_public_value(value: str) -> str:
    value = str(value or "")
    if len(value) > 120 or any(ch in value for ch in ("\r", "\n", "\x00")):
        return "<present>"
    return value


def sanitize_url(raw: str) -> str:
    """Keep useful public routing identifiers while removing secret values."""
    try:
        parsed = urlsplit(str(raw or ""))
    except Exception:
        return ""

    low = str(raw or "").lower()
    sensitive_flow = any(marker in low for marker in _SENSITIVE_FLOW_MARKERS)
    query: list[tuple[str, str]] = []
    if not sensitive_flow:
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            low_key = key.lower()
            if low_key in _SENSITIVE_QUERY_KEYS or any(
                marker in low_key for marker in ("token", "secret", "credential", "tracking")
            ):
                safe = "<redacted>"
            elif low_key in _PUBLIC_QUERY_VALUES:
                safe = _bounded_public_value(value)
            else:
                safe = "<present>"
            query.append((key, safe))

    fragment = ""
    if parsed.fragment and not sensitive_flow:
        # SPA fragments can contain another query string.  The route itself is
        # useful; all fragment parameters are intentionally discarded.
        fragment = parsed.fragment.split("?", 1)[0][:160]

    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query),
            fragment,
        )
    )


def graphql_metadata(request: Any) -> dict[str, Any]:
    """Return non-sensitive GraphQL structure without retaining field values."""
    try:
        if "/graphql" not in str(request.url or "").lower():
            return {}
        raw = request.post_data
        if not raw:
            return {}
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return {}
        operation = str(payload.get("operationName") or "")[:100]
        query = str(payload.get("query") or "")
        if not operation:
            match = _GRAPHQL_FIELD.search(query)
            operation = match.group(1) if match else "anonymous"
        variables = payload.get("variables")
        variable_keys = sorted(str(key)[:80] for key in variables) if isinstance(variables, dict) else []
        return {"operation": operation, "variable_keys": variable_keys}
    except Exception:
        return {"operation": "unreadable"}


def _sanitize_public_json(value: Any, *, depth: int = 0) -> Any:
    """Redact personal/session fields while preserving public catalog data."""
    if depth > 40:
        return "<depth-limit>"
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            low_key = re.sub(r"[^a-z0-9]", "", text_key.lower())
            if low_key in _SENSITIVE_JSON_KEYS or any(
                marker in low_key for marker in _SENSITIVE_JSON_KEY_MARKERS
            ):
                clean[text_key] = "<redacted>"
            else:
                clean[text_key] = _sanitize_public_json(item, depth=depth + 1)
        return clean
    if isinstance(value, list):
        return [_sanitize_public_json(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return value if len(value) <= _MAX_PUBLIC_JSON_BYTES else value[:_MAX_PUBLIC_JSON_BYTES]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]


def public_catalog_json(response: Any, metadata: dict[str, Any]) -> Any | None:
    """Read only known-public catalog JSON, never application/auth responses."""
    if metadata.get("operation") not in _PUBLIC_CATALOG_OPERATIONS:
        return None
    try:
        raw = response.body()
        if not raw or len(raw) > _MAX_PUBLIC_JSON_BYTES:
            return None
        return _sanitize_public_json(json.loads(raw.decode("utf-8")))
    except Exception:
        return None


class SafeResearchTrace:
    """Append sanitized context-level browser events to one local JSONL file."""

    def __init__(self, context: Any, path: str | Path) -> None:
        self.context = context
        self.path = Path(path)
        self.started = time.perf_counter()
        self._lock = threading.Lock()
        self._active = False
        self._page_handlers: list[tuple[Any, str, Any]] = []
        self._context_handlers: list[tuple[str, Any]] = []

    def _write(self, event: str, **fields: Any) -> None:
        record = {
            "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "elapsed_ms": round((time.perf_counter() - self.started) * 1000, 3),
            "event": event,
            **fields,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    @staticmethod
    def _resource_type(request: Any) -> str:
        try:
            return str(request.resource_type or "")[:40]
        except Exception:
            return ""

    def _on_request(self, request: Any) -> None:
        try:
            self._write(
                "request",
                method=str(request.method or "")[:12],
                url=sanitize_url(request.url),
                resource_type=self._resource_type(request),
                **graphql_metadata(request),
            )
        except Exception:
            pass

    def _on_response(self, response: Any) -> None:
        try:
            request = response.request
            metadata = graphql_metadata(request)
            headers = {
                str(key).lower(): str(value)[:300]
                for key, value in dict(response.headers or {}).items()
                if str(key).lower() in _SAFE_RESPONSE_HEADERS
            }
            self._write(
                "response",
                method=str(request.method or "")[:12],
                url=sanitize_url(response.url),
                resource_type=self._resource_type(request),
                status=int(response.status),
                safe_headers=headers,
                public_catalog_json=public_catalog_json(response, metadata),
                **metadata,
            )
        except Exception:
            pass

    def _on_request_failed(self, request: Any) -> None:
        try:
            self._write(
                "request_failed",
                method=str(request.method or "")[:12],
                url=sanitize_url(request.url),
                resource_type=self._resource_type(request),
                **graphql_metadata(request),
            )
        except Exception:
            pass

    def _attach_page(self, page: Any) -> None:
        try:
            self._write("page_opened", url=sanitize_url(getattr(page, "url", "")))

            def navigated(frame: Any) -> None:
                try:
                    if frame == page.main_frame:
                        self._write("page_navigated", url=sanitize_url(frame.url))
                except Exception:
                    pass

            def dom_ready() -> None:
                self._write("domcontentloaded", url=sanitize_url(getattr(page, "url", "")))

            def loaded() -> None:
                self._write("page_loaded", url=sanitize_url(getattr(page, "url", "")))

            for event, handler in (
                ("framenavigated", navigated),
                ("domcontentloaded", dom_ready),
                ("load", loaded),
            ):
                page.on(event, handler)
                self._page_handlers.append((page, event, handler))
        except Exception:
            pass

    def start(self) -> "SafeResearchTrace":
        if self._active:
            return self
        self._active = True
        self.path.unlink(missing_ok=True)
        self._write(
            "trace_started",
            policy=(
                "passive metadata only; no cookies, storage, authorization, request headers, "
                "application/auth/KYC bodies, OTP/PIN, CAPTCHA, or KYC identifiers; sanitized "
                "response JSON is retained only for public job-catalog operations"
            ),
        )
        for event, handler in (
            ("request", self._on_request),
            ("response", self._on_response),
            ("requestfailed", self._on_request_failed),
            ("page", self._attach_page),
        ):
            self.context.on(event, handler)
            self._context_handlers.append((event, handler))
        for page in list(getattr(self.context, "pages", ()) or ()):
            self._attach_page(page)
        return self

    def stop(self) -> None:
        if not self._active:
            return
        self._write("trace_stopped")
        self._active = False
        for page, event, handler in self._page_handlers:
            try:
                page.remove_listener(event, handler)
            except Exception:
                pass
        for event, handler in self._context_handlers:
            try:
                self.context.remove_listener(event, handler)
            except Exception:
                pass
        self._page_handlers.clear()
        self._context_handlers.clear()
