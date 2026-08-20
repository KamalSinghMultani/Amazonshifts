"""Prove the Hiring application session without reserving a shift.

Usage:
    python verify_session.py --config config.yaml
    python verify_session.py --config config.yaml --force-fresh-login

Default behavior mirrors the live watcher: strongly prove the saved session
first and only run the existing login flow if that proof fails. The optional
flag deliberately forces a new login for a targeted authentication test.
Neither mode creates an application or reserves a shift.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import session_refresh


def _brief(items, limit=12):
    values = [str(x) for x in (items or []) if x]
    if not values:
        return "<none>"
    shown = values[:limit]
    suffix = f" (+{len(values) - limit} more)" if len(values) > limit else ""
    return ", ".join(shown) + suffix


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--force-fresh-login",
        action="store_true",
        help="skip saved-session proof and exercise the existing login flow",
    )
    args = parser.parse_args(argv)

    state_dir = Path("state")
    output_state = state_dir / "verified_session_state.json"
    result_path = state_dir / "verified_session_result.json"

    rc = session_refresh.run(
        args.config,
        output_state=output_state,
        result_path=result_path,
        force_login=args.force_fresh_login,
    )

    try:
        result = json.loads(result_path.read_text("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"SESSION PROOF FAILED — could not read result: {exc}")
        return rc or 2

    status = result.get("status")
    detail = result.get("detail") or ""
    proof = result.get("proof") or {}
    diagnostics = result.get("auth_diagnostics") or {}
    precheck = result.get("precheck") or {}
    evidence = result.get("auth_evidence") or {}

    if rc == 0 and status in ("ok", "healthy") and proof.get("passed") is True:
        print("SESSION PROOF PASSED")
        print(
            "  path:               "
            + ("fresh login requested" if args.force_fresh_login else (
                "saved session strongly verified" if status == "healthy"
                else "saved proof failed; login recovered session"
            ))
        )
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
    if precheck:
        print(f"  saved-session proof: {precheck.get('reason') or 'failed'}")
    if diagnostics:
        print(f"  failure category:   {diagnostics.get('category')}")
        print(f"  returned state:     {diagnostics.get('returned_state')}")
        print(f"  machine state:      {diagnostics.get('machine_state')}")
        print(f"  challenge type:     {diagnostics.get('challenge_type')}")
        print(f"  final host:         {diagnostics.get('final_host')}")
    if proof:
        print(f"  expected host:      {proof.get('expected_host')}")
        print(f"  authenticated host: {proof.get('authenticated_host')}")
        print(f"  auth state:         {proof.get('authenticated_state')}")
        print(f"  application host:   {proof.get('application_host')}")
        print(f"  login redirect:     {proof.get('application_redirected_to_login')}")
    if evidence:
        print("  safe page evidence:")
        print(f"    title:             {evidence.get('title') or '<none>'}")
        print(f"    path:              {evidence.get('path') or '<none>'}")
        print(f"    login controls:    {evidence.get('login_controls_visible')}")
        print(f"    account marker:    {evidence.get('account_text_marker_visible')}")
        print(f"    application action:{evidence.get('application_action_visible')}")
        print(f"    visible test ids:  {_brief(evidence.get('visible_test_ids'))}")
        print(f"    visible element ids: {_brief(evidence.get('visible_element_ids'))}")
        print(f"    localStorage keys: {_brief(evidence.get('local_storage_keys'))}")
        print(f"    sessionStorage keys: {_brief(evidence.get('session_storage_keys'))}")
        print(f"    cookie names:      {_brief(evidence.get('cookie_names'))}")
        print("    note: values/credentials/tokens are intentionally not printed")
    return rc or 2


if __name__ == "__main__":
    raise SystemExit(main())