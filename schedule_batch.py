"""Batch searchScheduleCards calls into one GraphQL request.

Amazon requires a jobId for searchScheduleCards, but GraphQL aliases let one
HTTP request ask for several jobIds. This avoids an N+1 burst when a large job
batch lands while preserving schedule-level validity checks.
"""

from __future__ import annotations

from typing import Iterable

import schedules

SCHEDULE_FIELDS = """
      jobId
      scheduleId
      externalJobTitle
      city
      state
      siteId
      scheduleText
      scheduleType
      employmentType
      hoursPerWeek
      totalPayRate
      firstDayOnSite
      laborDemandAvailableCount
      laborDemandHardMatchCount
      __typename
"""


def _request_value(job_id: str, country: str, locale: str, page_size: int) -> dict:
    return {
        "jobId": job_id,
        "locale": locale,
        "country": country,
        "keyWords": "",
        "equalFilters": [],
        "containFilters": [{"key": "isPrivateSchedule", "val": ["false"]}],
        "rangeFilters": [],
        "orFilters": [],
        "dateFilters": [],
        "sorters": [],
        "pageSize": page_size,
        "consolidateSchedule": True,
    }


def build_request(
    job_ids: Iterable[str],
    *,
    country: str = "Canada",
    locale: str = "en-CA",
    page_size: int = 100,
) -> tuple[dict, list[str]]:
    """Return one aliased GraphQL request and the normalized job-id order."""
    ids = list(dict.fromkeys(str(job_id) for job_id in job_ids if job_id))
    variables: dict[str, dict] = {}
    declarations: list[str] = []
    selections: list[str] = []

    for index, job_id in enumerate(ids):
        name = f"r{index}"
        alias = f"s{index}"
        variables[name] = _request_value(job_id, country, locale, page_size)
        declarations.append(f"${name}: SearchScheduleRequest!")
        selections.append(
            f"""{alias}: searchScheduleCards(searchScheduleRequest: ${name}) {{
    nextToken
    scheduleCards {{
{SCHEDULE_FIELDS}
    }}
    __typename
  }}"""
        )

    query = "query batchScheduleCards"
    if declarations:
        query += "(" + ", ".join(declarations) + ")"
    query += " {\n  " + "\n  ".join(selections) + "\n}"

    return {
        "operationName": "batchScheduleCards",
        "variables": variables,
        "query": query,
    }, ids


def parse(payload: dict, job_ids: list[str]) -> dict[str, list[schedules.Schedule]]:
    """Map each requested jobId to the Schedule objects returned for it."""
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    out: dict[str, list[schedules.Schedule]] = {}

    for index, job_id in enumerate(job_ids):
        node = data.get(f"s{index}")
        cards = node.get("scheduleCards") if isinstance(node, dict) else []
        if not isinstance(cards, list):
            cards = []
        out[job_id] = [schedules.Schedule(card) for card in cards if isinstance(card, dict)]
    return out


def fetch(client, job_ids: Iterable[str], *, chunk_size: int = 20) -> dict[str, list[schedules.Schedule]]:
    """Fetch schedules for many jobs with a small number of GraphQL requests.

    Uses ApiClient._post_json intentionally so token refresh, cookies, headers,
    timeouts and HTTP error handling stay identical to ordinary polling.
    """
    ids = list(dict.fromkeys(str(job_id) for job_id in job_ids if job_id))
    out: dict[str, list[schedules.Schedule]] = {}
    chunk_size = max(1, int(chunk_size))

    for start in range(0, len(ids), chunk_size):
        chunk = ids[start:start + chunk_size]
        request, ordered = build_request(
            chunk,
            country=getattr(client, "country", None) or "Canada",
            locale=getattr(client, "locale", None) or "en-CA",
        )
        response = client._post_json(request)
        out.update(parse(response, ordered))
    return out
