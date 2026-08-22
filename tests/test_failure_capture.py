from __future__ import annotations

import json

import failure_capture


class FakePage:
    url = "https://hiring.amazon.ca/application/ca/?jobId=SECRET-JOB#consent"

    def screenshot(self, path, full_page=True):
        with open(path, "wb") as handle:
            handle.write(b"PNG")


def test_failure_capture_writes_png_and_safe_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(failure_capture, "SCREENSHOT_DIR", tmp_path)
    monkeypatch.setattr(
        failure_capture.auth_evidence,
        "collect",
        lambda *_args, **_kwargs: {
            "host": "hiring.amazon.ca",
            "local_storage_keys": ["accessToken", "idToken"],
            "cookie_names": ["HVH_ACCESS_TOKEN"],
        },
    )

    result = failure_capture.capture(
        FakePage(), None, "https://hiring.amazon.ca", "session-proof-failed"
    )

    assert result["screenshot"]
    assert result["sidecar"]
    payload = json.loads((tmp_path / result["sidecar"].split("/")[-1]).read_text("utf-8"))
    assert "SECRET-JOB" not in payload["safe_location"]
    assert payload["safe_location"].endswith("/application/ca/#consent")
    assert "accessToken" in payload["evidence"]["local_storage_keys"]
