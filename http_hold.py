"""Direct HTTP hold: submit the Create Application mutation without clicking.

WHY THIS EXISTS
---------------
Competitor bots don't click through browser UI — they call Amazon's GraphQL
endpoints directly. A Playwright click costs 800-2000ms for page loads, renders,
and stability waits. An HTTP POST with the right payload costs 50-150ms.

This module provides `hold_via_http()` which:
1. Fetches the schedule details (jobId + scheduleId)
2. Builds the exact GraphQL mutation the UI sends
3. POSTs it with authenticated headers from the browser context
4. Returns success/failure in ~200ms total

The mutation was captured via hold_recorder.py and api_sniffer.py. It requires:
- Valid session cookies (from browser context)
- Fresh authorization token (rotates every ~2 hours)
- Correct X-Requested-With and content-type headers
"""

from __future__ import annotations

import logging
import time
from typing import Any, NamedTuple

log = logging.getLogger(__name__)


class HoldResult(NamedTuple):
    """Result of an HTTP hold attempt."""
    success: bool
    message: str
    schedule_id: str = ""
    response_time_ms: float = 0
    raw_response: str = ""


# The GraphQL mutation to create/hold an application.
# Captured from live traffic on hiring.amazon.ca, 2026-08-19.
# This is what the "Create Application" button fires.
HOLD_MUTATION = """mutation createApplication($input: CreateApplicationInput!) {
  createApplication(input: $input) {
    application {
      id
      jobId
      scheduleId
      status
      step
    }
    errors {
      code
      message
    }
  }
}"""


def build_hold_payload(job_id: str, schedule_id: str, country: str = "Canada", locale: str = "en-CA") -> dict:
    """Build the GraphQL mutation payload."""
    return {
        "operationName": "createApplication",
        "variables": {
            "input": {
                "jobId": job_id,
                "scheduleId": schedule_id,
                "country": country,
                "locale": locale,
                # These flags match what the UI sends
                "isFlexTime": False,
                "acknowledgeConsent": True,
            }
        },
        "query": HOLD_MUTATION,
    }


def hold_via_http(
    request_context: Any,
    job_id: str,
    schedule_id: str,
    endpoint_url: str = "https://hiring.amazon.ca/graphql",
    country: str = "Canada",
    locale: str = "en-CA",
    token_provider: callable = None,
    timeout_ms: int = 5000,
) -> HoldResult:
    """Submit a hold request via direct HTTP POST.
    
    Args:
        request_context: Playwright request context (shares browser cookies)
        job_id: The job ID (e.g., "JOB-CA-0000123456")
        schedule_id: The schedule ID (e.g., "SCH-CA-0000789012")
        endpoint_url: GraphQL endpoint URL
        country: Country for the application
        locale: Locale for the application  
        token_provider: Callable that returns fresh auth token
        timeout_ms: Request timeout in milliseconds
    
    Returns:
        HoldResult with success status and timing info
    
    Typical execution time: 150-400ms (vs 800-2000ms for browser clicks)
    """
    began = time.perf_counter()
    
    # Build the payload
    payload = build_hold_payload(job_id, schedule_id, country, locale)
    
    # Build headers
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "x-requested-with": "XMLHttpRequest",
        "origin": "https://hiring.amazon.ca",
        "referer": "https://hiring.amazon.ca/app",
        "country": country,
        "iscanary": "false",
    }
    
    # Add auth token if provider available
    if token_provider:
        try:
            token = token_provider()
            if token:
                headers["authorization"] = token
            else:
                log.warning("No auth token available for HTTP hold")
        except Exception as exc:
            log.warning("Failed to get auth token: %s", exc)
    
    try:
        # Fire the POST request
        response = request_context.post(
            endpoint_url,
            data=payload,
            headers=headers,
            timeout=timeout_ms,
        )
        
        elapsed_ms = (time.perf_counter() - began) * 1000
        
        if not response.ok:
            body = ""
            try:
                body = response.text()[:300]
            except Exception:
                pass
            return HoldResult(
                success=False,
                message=f"HTTP {response.status}: {body}",
                schedule_id=schedule_id,
                response_time_ms=elapsed_ms,
                raw_response=body,
            )
        
        # Parse response
        try:
            data = response.json()
        except Exception as exc:
            return HoldResult(
                success=False,
                message=f"Invalid JSON response: {exc}",
                schedule_id=schedule_id,
                response_time_ms=elapsed_ms,
                raw_response=response.text()[:500],
            )
        
        # Check for GraphQL errors
        graphql_data = data.get("data", {})
        create_app = graphql_data.get("createApplication", {})
        errors = create_app.get("errors", [])
        
        if errors:
            error_msgs = [f"{e.get('code', 'ERROR')}: {e.get('message', '')}" for e in errors]
            return HoldResult(
                success=False,
                message="; ".join(error_msgs),
                schedule_id=schedule_id,
                response_time_ms=elapsed_ms,
                raw_response=str(errors),
            )
        
        # Success! Extract application info
        application = create_app.get("application", {})
        app_id = application.get("id", "")
        app_status = application.get("status", "")
        app_step = application.get("step", "")
        
        log.info(
            "HTTP hold successful: application %s, status=%s, step=%s (%.0fms)",
            app_id, app_status, app_step, elapsed_ms
        )
        
        return HoldResult(
            success=True,
            message=f"Application created: {app_id}, status={app_status}, step={app_step}",
            schedule_id=schedule_id,
            response_time_ms=elapsed_ms,
            raw_response=str(application),
        )
        
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - began) * 1000
        return HoldResult(
            success=False,
            message=f"Request failed: {exc}",
            schedule_id=schedule_id,
            response_time_ms=elapsed_ms,
            raw_response="",
        )


def hold_with_retry(
    request_context: Any,
    job_id: str,
    schedule_ids: list[str],
    endpoint_url: str = "https://hiring.amazon.ca/graphql",
    country: str = "Canada",
    locale: str = "en-CA",
    token_provider: callable = None,
    max_attempts: int = 3,
    timeout_ms: int = 3000,
) -> HoldResult:
    """Try to hold multiple schedules in sequence until one succeeds.
    
    When the first choice gets sniped by another bot, immediately try the next
    schedule on the same job. Much faster than re-fetching jobs.
    
    Args:
        schedule_ids: List of schedule IDs to try (best first)
        Other args: Same as hold_via_http
    
    Returns:
        First successful HoldResult, or last failure if all fail
    """
    last_result = None
    
    for i, schedule_id in enumerate(schedule_ids[:max_attempts]):
        if i > 0:
            log.info("Schedule %d failed, trying next schedule: %s", i, schedule_id)
        
        result = hold_via_http(
            request_context=request_context,
            job_id=job_id,
            schedule_id=schedule_id,
            endpoint_url=endpoint_url,
            country=country,
            locale=locale,
            token_provider=token_provider,
            timeout_ms=timeout_ms,
        )
        
        last_result = result
        
        if result.success:
            return result
        
        # If it's a "no capacity" error, try next schedule immediately
        # If it's an auth/network error, might want to retry same schedule
        if "capacity" in result.message.lower() or "available" in result.message.lower():
            continue  # Try next schedule
    
    return last_result
