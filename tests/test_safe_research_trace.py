from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import safe_research_trace


class FakeContext:
    def __init__(self):
        self.handlers = {}
        self.pages = []

    def on(self, event, handler):
        self.handlers[event] = handler

    def remove_listener(self, event, handler):
        if self.handlers.get(event) is handler:
            self.handlers.pop(event)


class FakeRequest:
    method = "POST"
    resource_type = "fetch"

    def __init__(self, url, post_data):
        self.url = url
        self.post_data = post_data


class FakeResponse:
    status = 200

    def __init__(self, request):
        self.request = request
        self.url = request.url
        self.headers = {
            "content-type": "application/json",
            "cache-control": "no-store",
            "set-cookie": "session=must-not-be-written",
            "authorization": "must-not-be-written",
        }

    def body(self):
        return json.dumps(
            {
                "data": {
                    "getJobDetail": {
                        "jobId": "JOB-1",
                        "postingStatus": "UNPOSTED",
                        "locationName": "Barrhaven, ON",
                        "trackingId": "body-tracking-secret",
                        "candidateId": "candidate-secret",
                    }
                }
            }
        ).encode("utf-8")


def test_url_sanitizer_keeps_public_ids_but_redacts_sensitive_values():
    safe = safe_research_trace.sanitize_url(
        "https://hiring.amazon.ca/graphql?jobId=JOB-1&scheduleId=SCH-2"
        "&email=person@example.com&trackingId=private-tracking&unknown=private"
    )
    values = parse_qs(urlsplit(safe).query)
    assert values["jobId"] == ["JOB-1"]
    assert values["scheduleId"] == ["SCH-2"]
    assert values["email"] == ["<redacted>"]
    assert values["trackingId"] == ["<redacted>"]
    assert values["unknown"] == ["<present>"]
    assert "person@example.com" not in safe
    assert "private-tracking" not in safe


def test_liveness_url_loses_all_query_and_fragment_identifiers():
    safe = safe_research_trace.sanitize_url(
        "https://hiring.amazon.ca/application/ca/?jobId=JOB-1&trackingId=secret"
        "#/liveness-check?trackingId=secret-two"
    )
    assert safe == "https://hiring.amazon.ca/application/ca/"
    assert "secret" not in safe


def test_trace_records_graphql_shape_and_status_without_replayable_material(tmp_path):
    context = FakeContext()
    path = tmp_path / "research.jsonl"
    trace = safe_research_trace.SafeResearchTrace(context, path).start()
    request = FakeRequest(
        "https://hiring.amazon.ca/graphql?jobId=JOB-1&token=url-secret",
        json.dumps(
            {
                "operationName": "getJobDetail",
                "query": "query getJobDetail($jobId: String!) { getJobDetail { jobId } }",
                "variables": {
                    "jobId": "JOB-1",
                    "authorization": "body-secret",
                    "email": "person@example.com",
                },
            }
        ),
    )
    context.handlers["request"](request)
    context.handlers["response"](FakeResponse(request))
    trace.stop()

    text = path.read_text("utf-8")
    records = [json.loads(line) for line in text.splitlines()]
    assert any(
        item.get("operation") == "getJobDetail"
        and item.get("variable_keys") == ["authorization", "email", "jobId"]
        for item in records
    )
    assert any(item.get("status") == 200 for item in records)
    response_record = next(
        item for item in records if item.get("event") == "response"
    )
    detail = response_record["public_catalog_json"]["data"]["getJobDetail"]
    assert detail["postingStatus"] == "UNPOSTED"
    assert detail["locationName"] == "Barrhaven, ON"
    assert detail["trackingId"] == "<redacted>"
    assert detail["candidateId"] == "<redacted>"
    assert "JOB-1" in text
    assert "url-secret" not in text
    assert "body-secret" not in text
    assert "person@example.com" not in text
    assert "body-tracking-secret" not in text
    assert "candidate-secret" not in text
    assert "must-not-be-written" not in text
    assert "set-cookie" not in text
    assert "authorization\"" in text  # variable name only, never its value


def test_trace_never_reads_non_catalog_application_response_body(tmp_path):
    class PrivateResponse(FakeResponse):
        def body(self):
            raise AssertionError("private response body must not be read")

    context = FakeContext()
    path = tmp_path / "research.jsonl"
    trace = safe_research_trace.SafeResearchTrace(context, path).start()
    request = FakeRequest(
        "https://hiring.amazon.ca/graphql",
        json.dumps(
            {
                "operationName": "createApplication",
                "variables": {"candidateId": "private"},
            }
        ),
    )
    context.handlers["response"](PrivateResponse(request))
    trace.stop()

    records = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    response = next(item for item in records if item.get("event") == "response")
    assert response["operation"] == "createApplication"
    assert response["public_catalog_json"] is None
    assert "private" not in path.read_text("utf-8")
