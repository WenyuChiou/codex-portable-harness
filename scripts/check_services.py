"""Explicit online checks; report booleans only, never credentials or notebook data."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def notebooklm(state_file: Path) -> dict:
    from research_hub.notebooklm.auth import check_session_health
    # The pinned SDK performs a server notebooks.list call. Its returned notebook
    # data and potentially sensitive exception reason are deliberately discarded.
    health = check_session_health(state_file)
    return {"service": "notebooklm", "authenticated_server_check": bool(health.get("ok"))}


def antigravity(executable: str) -> dict:
    result = subprocess.run([executable, "--sandbox", "--mode", "plan", "--print",
        "Reply exactly AGY_READY. Do not use tools or change files.", "--output-format", "json", "--print-timeout", "60s"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
    passed = False
    if result.returncode == 0:
        try:
            data = json.loads(result.stdout)
            passed = data.get("status") == "SUCCESS" and data.get("response", "").strip() == "AGY_READY"
        except ValueError:
            pass
    return {"service": "antigravity", "authenticated_canary": passed, "exit_code": result.returncode}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", action="store_true", help="Authorize online verification; AGY canary uses a paid model call")
    parser.add_argument("--notebooklm-state", type=Path, help="Explicit local state path; cookies are never printed")
    parser.add_argument("--antigravity", metavar="EXECUTABLE")
    args = parser.parse_args()
    if not args.network or not (args.notebooklm_state or args.antigravity):
        print(json.dumps({"status": "NOT_CHECKED", "reason": "Select services and pass --network; no provider call made"}))
        return 2
    checks = []
    for name, selected, action in (("notebooklm", args.notebooklm_state, lambda: notebooklm(args.notebooklm_state)),
                                    ("antigravity", args.antigravity, lambda: antigravity(args.antigravity))):
        if selected:
            try:
                checks.append(action())
            except Exception as exc:
                checks.append({"service": name, "passed": False, "error_type": type(exc).__name__})
    passed = all(check.get("authenticated_server_check", check.get("authenticated_canary", False)) for check in checks)
    print(json.dumps({"status": "PASS" if passed else "FAIL", "checks": checks}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
