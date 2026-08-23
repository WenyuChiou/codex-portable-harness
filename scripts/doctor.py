from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from bootstrap import desired_files
from common import REPO_ROOT, active_codex_home, ensure_within, load_json, sha256
from validators import validate_host_manifest


@dataclass
class Finding:
    status: str
    check: str
    detail: str


def indexed_texts(root: Path) -> list[tuple[Path, str]]:
    excluded = {".git", "__pycache__", ".pytest_cache", "generated", "artifacts"}
    try:
        result = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True)
        relative_paths = [Path(item.decode("utf-8")) for item in result.stdout.split(b"\0") if item]
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):
        relative_paths = [path.relative_to(root) for path in root.rglob("*") if path.is_file() and not path.is_symlink()]
        index_available = False
    else:
        index_available = True
    documents: list[tuple[Path, str]] = []
    for relative in relative_paths:
        if relative.is_absolute() or ".." in relative.parts or any(part in excluded for part in relative.parts):
            continue
        if index_available:
            result = subprocess.run(
                ["git", "-C", str(root), "show", f":{relative.as_posix()}"],
                capture_output=True,
            )
            if result.returncode != 0:
                continue
            raw = result.stdout
        else:
            path = root / relative
            ensure_within(path, root)
            raw = path.read_bytes()
        try:
            documents.append((relative, raw.decode("utf-8")))
        except UnicodeDecodeError:
            continue
    return sorted(documents, key=lambda item: item[0].as_posix())


def check_json_documents() -> list[Finding]:
    findings: list[Finding] = []
    required = [REPO_ROOT / "policies" / "agent-budget.yaml"]
    required.extend(sorted((REPO_ROOT / "schemas").glob("*.json")))
    required.append(REPO_ROOT / "manifests" / "host.example.json")
    for path in required:
        try:
            document = load_json(path)
            if path.name == "host.example.json":
                validate_host_manifest(document)
            findings.append(Finding("PASS", "structured-data", str(path.relative_to(REPO_ROOT))))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            findings.append(Finding("FAIL", "structured-data", f"{path}: {exc}"))
    return findings


def check_repo_content(root: Path = REPO_ROOT) -> list[Finding]:
    windows_home = re.compile(r"[A-Za-z]:" + re.escape("\\Users\\") + r"[^\\\s]+", re.IGNORECASE)
    posix_home = re.compile(r"/(?:Users|home)/[^/\s]+/")
    findings: list[Finding] = []
    for relative, text in indexed_texts(root):
        if contains_secret(text):
            findings.append(Finding("FAIL", "secret-scan", f"credential-shaped value in {relative}"))
        if windows_home.search(text) or posix_home.search(text):
            findings.append(Finding("FAIL", "absolute-path", str(relative)))
    if not any(item.check == "secret-scan" for item in findings):
        findings.append(Finding("PASS", "secret-scan", "no high-confidence credential patterns"))
    if not any(item.check == "absolute-path" for item in findings):
        findings.append(Finding("PASS", "absolute-path", "no user-specific absolute paths"))
    return findings


def contains_secret(text: str) -> bool:
    patterns = (
        re.compile("sk" + r"-[A-Za-z0-9_-]{20,}"),
        re.compile("gh" + r"p_[A-Za-z0-9]{20,}"),
        re.compile("gl" + r"pat-[A-Za-z0-9_-]{20,}"),
        re.compile("AK" + r"IA[0-9A-Z]{16}"),
        re.compile("AS" + r"IA[0-9A-Z]{16}"),
        re.compile("xo" + r"x[baprs]-[A-Za-z0-9-]{20,}"),
        re.compile("AI" + r"za[0-9A-Za-z_-]{35}"),
        re.compile("BEGIN " + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY"),
        re.compile("Bearer " + r"eyJ[A-Za-z0-9_-]{20,}"),
    )
    return any(pattern.search(text) for pattern in patterns)


def check_outer_ignore(home: Path) -> list[Finding]:
    probe_paths = [home / "auth.json", home / "sessions" / "probe.json", home / "state_5.sqlite", home / "config.toml"]
    outer = home.parent
    try:
        subprocess.run(["git", "-C", str(outer), "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError):
        return [Finding("WARN", "outer-git-ignore", "CODEX_HOME is not inside an outer Git worktree")]
    failures: list[str] = []
    for path in probe_paths:
        result = subprocess.run(["git", "-C", str(outer), "check-ignore", "--quiet", "--", str(path)], capture_output=True)
        if result.returncode != 0:
            failures.append(path.name)
    if failures:
        return [Finding("FAIL", "outer-git-ignore", f"not ignored: {', '.join(failures)}")]
    return [Finding("PASS", "outer-git-ignore", "runtime probes are ignored")]


def check_skill_deployment(home: Path) -> list[Finding]:
    drift: list[str] = []
    expected: set[str] = set()
    for source, relative in desired_files():
        expected.add(relative.as_posix())
        destination = home / relative
        if not destination.exists() or sha256(source) != sha256(destination):
            drift.append(relative.as_posix())
    actual: set[str] = set()
    for skill in ("adaptive-response", "bounded-agent-harness"):
        root = home / "skills" / skill
        if root.exists():
            actual.update(path.relative_to(home).as_posix() for path in root.rglob("*") if path.is_file())
    unexpected = sorted(actual.difference(expected))
    if unexpected:
        drift.append(f"unexpected: {', '.join(unexpected)}")
    if drift:
        return [Finding("FAIL", "skill-diff", f"deploy with bootstrap.py --apply: {', '.join(drift)}")]
    return [Finding("PASS", "skill-diff", "deployed skills match canonical sources")]


def check_exports() -> list[Finding]:
    expected = [
        REPO_ROOT / "exports" / "claude" / "ADAPTIVE_RESPONSE.proposal.md",
        REPO_ROOT / "exports" / "hermes" / "ADAPTIVE_RESPONSE.proposal.md",
    ]
    invalid: list[str] = []
    for path in expected:
        if not path.is_file():
            invalid.append(str(path.relative_to(REPO_ROOT)))
            continue
        text = path.read_text(encoding="utf-8").lower()
        if "proposal" not in text or "do not apply automatically" not in text:
            invalid.append(f"missing proposal-only banner: {path.relative_to(REPO_ROOT)}")
    return [Finding("FAIL" if invalid else "PASS", "cross-tool-exports", ", ".join(invalid) if invalid else "proposal-only exports present")]


def host_capabilities(home: Path) -> dict[str, object]:
    system = platform.system().lower()
    os_name = "windows" if system == "windows" else "macos" if system == "darwin" else "linux"
    capabilities = {
        "schema_version": 1,
        "host_id": platform.node() or "unknown",
        "os": os_name,
        "shell": os.environ.get("SHELL") or os.environ.get("COMSPEC") or "unknown",
        "path_style": "windows" if os_name == "windows" else "posix",
        "sandbox": "unknown",
        "pty": bool(sys.stdin.isatty()),
        "gui": bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or os_name in {"windows", "macos"}),
        "roots": {"codex_home": str(home)},
        "available_tools": [],
    }
    validate_host_manifest(capabilities)
    return capabilities


def run(home: Path, cross_tool: bool, diff: bool) -> tuple[list[Finding], dict[str, object]]:
    findings = check_json_documents() + check_repo_content() + check_outer_ignore(home)
    if diff:
        findings.extend(check_skill_deployment(home))
    if cross_tool:
        findings.extend(check_exports())
    return findings, host_capabilities(home)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate portable Codex harness containment and deployment.")
    parser.add_argument("--cross-tool", action="store_true")
    parser.add_argument("--diff", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    home = active_codex_home()
    findings, capabilities = run(home, args.cross_tool, args.diff)
    if args.json:
        print(json.dumps({"findings": [asdict(item) for item in findings], "host": capabilities}, ensure_ascii=False, indent=2))
    else:
        for item in findings:
            print(f"{item.status:4} {item.check}: {item.detail}")
        print(f"HOST {capabilities['os']} shell={capabilities['shell']} codex_home={home}")
    return 1 if any(item.status == "FAIL" for item in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
