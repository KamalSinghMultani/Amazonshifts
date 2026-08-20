"""Force a fresh Hiring login and print non-destructive session proof.

Usage:
    python verify_session.py
    python verify_session.py --config config.yaml

This never creates an application or reserves a shift. It proves a fresh login
on the configured country host and application-shell access, then writes the
same storage state the watcher can import.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import session_refresh


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)

    state_dir = Path("state")
    output_state = state_dir / "verified_session_state.json"
    result_path = state_dir / "verified_session_result.json"

    rc = session_refresh.run(
        args.config,
        output_state=output_state,
        result_path=result_path,
        force_login=True,
    )

    try:
        result = json.loads(result_path.read_text("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"SESSION PROOF FAILED — could not read result: {exc}")
        return rc or 2

    status = result.get("status")
    detail = result.get("detail") or ""
    proof = result.get("proof") or {}

    if rc == 0 and status == "ok" and proof.get("passed") is True:
        print("SESSION PROOF PASSED")
        print(f"  expected host:      {proof.get('expected_host')}")
        print(f"  authenticated host: {proof.get('authenticated_host')}")
        print(f"  auth state:         {proof.get('authenticated_state')}")
        print(f"  application host:   {proof.get('application_host')}")
        print("  login redirect:     no")
        print("  destructive action: none")
        print(f"  detail:             {detail}")
        return 0

    print("SESSION PROOF FAILED")
    print(f"  status: {status}")
    print(f"  detail: {detail}")
    if proof:
        print(f"  expected host:      {proof.get('expected_host')}")
        print(f"  authenticated host: {proof.get('authenticated_host')}")
        print(f"  auth state:         {proof.get('authenticated_state')}")
        print(f"  application host:   {proof.get('application_host')}")
        print(f"  login redirect:     {proof.get('application_redirected_to_login')}")
    return rc or 2


if __name__ == "__main__":
    raise SystemExit(main())
