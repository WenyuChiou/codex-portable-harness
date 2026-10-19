"""Read-only local checks; native sandbox checks are explicit and never use a model."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path

from bootstrap import _reject_symlinks
from common import REPO_ROOT, active_codex_home, atomic_write_json, load_json, sha256
from machine_profile import relative_path
from setup_machine import python_at
from doctor import check_skill_deployment


def check_ownership(home: Path, manifest: Path) -> dict:
    _reject_symlinks(manifest, home)
    if not manifest.is_file():
        return {"check": "research-files", "status": "FAIL", "detail": "Research profile has not recorded ownership"}
    data = load_json(manifest)
    if data.get("schema_version") != 1 or not data.get("files"):
        raise ValueError("empty or invalid ownership manifest")
    mismatches = []
    for name, digest in data["files"].items():
        relative_path(name)
        target = home / name
        _reject_symlinks(target, home)
        if not target.is_file() or sha256(target) != digest:
            mismatches.append(name)
    return {"check": "research-files", "status": "FAIL" if mismatches else "PASS",
            "count": len(data["files"]), "mismatches": mismatches}


def command_check(name: str, command: list[str], *, timeout: int = 90) -> dict:
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        entry = {"check": name, "status": "PASS" if result.returncode == 0 else "FAIL", "exit_code": result.returncode}
        # Only our response-only probe output is retained. No provider/auth diagnostics.
        if result.returncode == 0 and "python" in name:
            entry["probe"] = json.loads(result.stdout)
        return entry
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"check": name, "status": "FAIL", "error_type": type(exc).__name__}


def check_compatibility(home: Path, site: Path) -> dict:
    manifest = home / "machine-setup/compatibility/notebooklm-auth.upstream.json"
    original = manifest.with_suffix(".py")
    target = site / "research_hub/notebooklm/auth.py"
    for path in (manifest, original, target):
        _reject_symlinks(path, home)
    known = load_json(manifest)
    matches = original.is_file() and target.is_file() and sha256(original) == known["upstream_sha256"] and sha256(target) == known["patched_sha256"]
    return {"check": "notebooklm-compatibility", "status": "PASS" if matches else "FAIL"}


def verify(home: Path, *, sandbox: bool, codex: str = "codex") -> dict:
    findings = [asdict(item) for item in check_skill_deployment(home)]
    findings.append(check_ownership(home, home / "machine-setup/research-ownership.json"))
    expected = ["portable-mechanical", "portable-mechanical-readonly", "portable-explorer",
                "portable-quality-explorer", "portable-worker", "code-reviewer",
                "banned-word-auditor", "harness-drift-monitor", "silent-failure-hunter-py"]
    missing = [name for name in expected if not (home / "agents" / (name + ".toml")).is_file()]
    findings.append({"check": "agents", "status": "FAIL" if missing else "PASS", "missing": missing})
    for label, directory, research in (("harness", "portable-harness-runtime", False), ("research", "research-runtime", True)):
        environment = home / directory
        command = [str(python_at(environment)), "-B", str(REPO_ROOT / "scripts/runtime_probe.py"), "--expected", str(environment)]
        if research:
            command.append("--research")
        findings.append(command_check(label + "-python", command))
        if sandbox:
            if os.name != "nt":
                findings.append({"check": label + "-sandbox-python", "status": "SKIP", "detail": "Use native agent suite for this platform"})
            else:
                findings.append(command_check(label + "-sandbox-python", [codex, "sandbox", "-c", 'windows.sandbox="elevated"',
                    "--permission-profile", ":workspace", "-C", str(home), "--", *command], timeout=120))
    defuddle = home / "research-node/node_modules/.bin" / ("defuddle.cmd" if os.name == "nt" else "defuddle")
    research_probe = next((item.get("probe") for item in findings if item.get("check") == "research-python"), None)
    if research_probe and research_probe.get("purelib"):
        findings.append(check_compatibility(home, Path(research_probe["purelib"])))
    agy = shutil.which("agy")
    installation = home / "machine-setup/installation.json"
    if installation.exists():
        agy = load_json(installation).get("antigravity", {}).get("executable") or agy
    findings += [command_check("defuddle", [str(defuddle), "--version"]),
                 command_check("antigravity-binary", [agy or "agy", "--version"], timeout=30)]
    # Presence is never called authenticated success; no cookies or keys are read.
    pending = ["Antigravity authenticated canary", "NotebookLM login and read-only server call", "Zotero credentials (optional)"]
    return {"schema_version": 1, "local_checks_passed": not any(f["status"] == "FAIL" for f in findings),
            "findings": findings, "authenticated_services": "NOT_CHECKED", "pending": pending,
            "native_agents": "NOT_CHECKED; run agent_smoke.py --run"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox", action="store_true")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--save", action="store_true", help="Save a host-local report inside CODEX_HOME")
    args = parser.parse_args()
    try:
        home = active_codex_home()
        result = verify(home, sandbox=args.sandbox, codex=args.codex)
        if args.save:
            atomic_write_json(home / "machine-setup/verification.json", result)
        print(json.dumps(result, indent=2))
        return 0 if result["local_checks_passed"] else 1
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "FAIL", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
