"""Isolated session guard worker used by watcher_v5.

Two modes are intentionally separate:

* prove   - prove the supplied current session only. Never attempts a login.
* recover - run the existing login/recovery state machine and strongly prove
            the resulting session before handing it back.

This keeps a harmless health check from turning into an authentication attempt
that can trigger a challenge. No CAPTCHA solving behavior is implemented here;
recovery reuses the project's existing session_refresh/login machinery.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

import browser_launch
import session_proof
import session_refresh
from config import load_config, load_dotenv


def _write(path: Path, **payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), "utf-8")


def _prove(config_path: str, input_state: Path, output_state: Path, result_path: Path) -> int:
    load_dotenv()
    cfg = load_config(config_path)
    browser_cfg = dict(cfg["browser"])
    browser_cfg["user_data_dir"] = None
    browser_cfg["headless"] = True
    base_url = cfg["site"]["base_url"]

    try:
        with sync_playwright() as playwright:
            browser, context = browser_launch.launch_context(
                playwright,
                browser_cfg,
                storage_state=str(input_state) if input_state.exists() else None,
            )
            context.set_default_timeout(browser_cfg["action_timeout_ms"])
            context.set_default_navigation_timeout(browser_cfg["nav_timeout_ms"])
            page = context.pages[0] if context.pages else context.new_page()
            proof = session_proof.prove_existing_session(page, base_url, settle_ms=1500)
            if proof.passed:
                output_state.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(output_state))
                _write(
                    result_path,
                    status="healthy",
                    detail=proof.reason + "; " + proof.summary(),
                    proof=proof.to_dict(),
                    definitive_expiry=False,
                    mode="prove",
                )
                browser_launch.close_context(browser, context)
                return 0

            # A real auth redirect or a 401 from the protected candidate read is
            # authoritative expiry evidence. Slow React, missing response events,
            # network errors, and WAF/403 responses stay inconclusive so a health
            # check never turns into an unnecessary authentication attempt.
            definitive = bool(
                proof.application_redirected_to_login
                or proof.application_backend_unauthorized
            )
            _write(
                result_path,
                status="expired" if definitive else "inconclusive",
                detail=proof.reason + "; " + proof.summary(),
                proof=proof.to_dict(),
                definitive_expiry=definitive,
                mode="prove",
            )
            browser_launch.close_context(browser, context)
            return 2
    except Exception as exc:  # noqa: BLE001 - worker reports, never exposes request headers
        _write(
            result_path,
            status="error",
            detail=f"session proof worker failed ({type(exc).__name__})",
            definitive_expiry=False,
            mode="prove",
        )
        return 3


def _runtime_config(config_path: str, input_state: Path, destination: Path) -> Path:
    """Write a temporary config whose recovery seed is the live context state."""
    cfg = copy.deepcopy(load_config(config_path))
    cfg["browser"]["storage_state"] = str(input_state)
    cfg["browser"]["user_data_dir"] = None
    (cfg.get("polling") or {}).pop("hot_windows_parsed", None)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(yaml.safe_dump(cfg, sort_keys=False), "utf-8")
    return destination


def _recover(config_path: str, input_state: Path, output_state: Path, result_path: Path) -> int:
    runtime = result_path.with_name("session_guard_recovery_runtime.yaml")
    _runtime_config(config_path, input_state, runtime)
    return session_refresh.run(
        str(runtime),
        output_state=output_state,
        result_path=result_path,
        force_login=True,
    )


def run(
    config_path: str,
    input_state: Path,
    output_state: Path,
    result_path: Path,
    mode: str,
) -> int:
    if mode == "prove":
        return _prove(config_path, input_state, output_state, result_path)
    if mode == "recover":
        return _recover(config_path, input_state, output_state, result_path)
    _write(result_path, status="error", detail=f"unknown mode: {mode}", mode=mode)
    return 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--input-state", required=True)
    parser.add_argument("--output-state", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--mode", choices=("prove", "recover"), required=True)
    args = parser.parse_args(argv)
    return run(
        args.config,
        Path(args.input_state),
        Path(args.output_state),
        Path(args.result),
        args.mode,
    )


if __name__ == "__main__":
    raise SystemExit(main())
