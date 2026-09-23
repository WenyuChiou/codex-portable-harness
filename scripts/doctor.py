from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from bootstrap import desired_files
import model_promotion
from common import REPO_ROOT, active_codex_home, ensure_within, load_json, sha256
from validators import validate_host_manifest


PUBLIC_HARNESS_VERSION = "0.5.1"


@dataclass
class Finding:
    status: str
    check: str
    detail: str


def check_public_harness() -> list[Finding]:
    try:
        import agent_collab_harness
    except ImportError as exc:
        return [Finding("FAIL", "public-harness", f"agent-collab-harness unavailable: {exc}")]
    version = getattr(agent_collab_harness, "__version__", None)
    if version != PUBLIC_HARNESS_VERSION:
        return [
            Finding(
                "FAIL",
                "public-harness",
                f"expected agent-collab-harness {PUBLIC_HARNESS_VERSION}, found {version!r}",
            )
        ]
    from agent_collab_harness.doctor import run_doctor
    result = run_doctor()
    if result["status"] != "ok":
        return [Finding("FAIL", "public-harness", "shared package schema or configuration failure")]
    return [Finding("PASS", "public-harness", f"agent-collab-harness {version}; schemas loaded")]


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
            if path.name == "agent-budget.yaml":
                from agent_collab_harness import validate_policy
                validate_policy(document)
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
    probe_paths = [home / "auth.json", home / "sessions" / "probe.json", home / "state_5.sqlite", home / "config.toml",
                   home / "harness-runs" / "probe.json", home / "harness-attestations" / "probe.json",
                   home / "experiments" / "model-role-promotion" / "probe.json"]
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
    for source, relative in desired_files(home):
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
    policy = load_json(REPO_ROOT / "policies" / "model-routing.json")
    for candidate in policy["role_promotion"]["candidates"].values():
        for role in model_promotion.candidate_roles(candidate):
            relative = (Path("agents") / f"{role}.toml").as_posix()
            if relative not in expected and (home / relative).exists():
                drift.append(f"stale candidate: {relative}")
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


def check_agent_configuration() -> list[Finding]:
    """Check our deliberately small standalone-agent subset on Python 3.10+."""
    try:
        policy = load_json(REPO_ROOT / "policies" / "model-routing.json")
        for category, route in policy["routes"].items():
            roles = [route["role"]]
            if category == "mechanical":
                roles.append(route["read_only_role"])
            for role in roles:
                path = REPO_ROOT / "agents" / (role + ".toml")
                text = path.read_text(encoding="utf-8")
                for key, expected in (("name", role), ("model", route["model"]),
                                      ("model_reasoning_effort", route["effort"])):
                    found = re.findall(r'^' + re.escape(key) + r'\s*=\s*"([^"\r\n]+)"\s*$', text, re.MULTILINE)
                    if found != [expected]:
                        raise ValueError(f"{path.name}: {key} differs from canonical routing")
                if re.search(r"^(?:service_tier|approval_policy)\s*=", text, re.MULTILINE):
                    raise ValueError(f"{path.name}: forbidden tier/approval override")
                if 'developer_instructions = """' not in text:
                    raise ValueError(f"{path.name}: missing instructions")
                if category == "exploration" or role == route.get("read_only_role"):
                    if re.findall(r'^sandbox_mode\s*=\s*"([^"\r\n]+)"\s*$', text, re.MULTILINE) != ["read-only"]:
                        raise ValueError(f"{path.name}: read-only route lacks a native sandbox")
        escalation = policy["quality_escalation"]
        if (
            type(escalation["enabled"]) is not bool
            or not isinstance(escalation["model"], str)
            or not escalation["model"].strip()
            or escalation["effort"] not in {"low", "medium", "high", "xhigh", "max"}
            or escalation["role"] != "default"
            or escalation["exploration_role"] != "portable-quality-explorer"
            or escalation["review_role"] != "code-reviewer"
            or not isinstance(escalation["reasons"], dict)
            or not escalation["reasons"]
        ):
            raise ValueError("quality escalation configuration is invalid")
        allowed_classes = {"exploration", "implementation", "review"}
        for reason, classes in escalation["reasons"].items():
            if not isinstance(reason, str) or not reason.strip() or not isinstance(classes, list) or not classes or any(item not in allowed_classes for item in classes):
                raise ValueError("quality escalation reason classes are invalid")
        quality_path = REPO_ROOT / "agents" / (escalation["exploration_role"] + ".toml")
        quality_text = quality_path.read_text(encoding="utf-8")
        for key, expected in (("name", escalation["exploration_role"]),
                              ("model", escalation["model"]),
                              ("model_reasoning_effort", escalation["effort"]),
                              ("sandbox_mode", "read-only")):
            found = re.findall(r'^' + re.escape(key) + r'\s*=\s*"([^"\r\n]+)"\s*$', quality_text, re.MULTILINE)
            if found != [expected]:
                raise ValueError(f"{quality_path.name}: {key} differs from canonical quality route")
        if re.search(r"^(?:service_tier|approval_policy)\s*=", quality_text, re.MULTILINE):
            raise ValueError(f"{quality_path.name}: forbidden tier/approval override")
        if 'developer_instructions = """' not in quality_text:
            raise ValueError(f"{quality_path.name}: missing instructions")
        primary = policy["primary_preference"]
        if primary != {"source": "active_codex_session", "changes_host_model_setting": False}:
            raise ValueError("primary preference must inherit the active session")
        promotion = policy["role_promotion"]
        if (promotion["minimum_paired_runs"] < 8 or promotion["lifetime_days"] < 1
            or promotion["maximum_wall_regression_percent"] > 15
            or promotion["maximum_cost_proxy_regression_percent"] > 15):
            raise ValueError("promotion thresholds are weaker than the approved gate")
        for category, candidate in promotion["candidates"].items():
            if category not in policy["routes"] or candidate["model"] not in promotion["api_usd_per_million_tokens"]:
                raise ValueError("candidate route or price is missing")
            for role in model_promotion.candidate_roles(candidate):
                path = model_promotion.candidate_agent_source(role)
                text = path.read_text(encoding="utf-8")
                for key, expected in (("name", role), ("model", candidate["model"]),
                                      ("model_reasoning_effort", candidate["effort"])):
                    if re.findall(r'^' + re.escape(key) + r'\s*=\s*"([^"\r\n]+)"\s*$', text, re.MULTILINE) != [expected]:
                        raise ValueError(f"{path.name}: {key} differs from candidate route")
                if (category == "exploration" or role == candidate.get("read_only_role")) and re.findall(
                    r'^sandbox_mode\s*=\s*"([^"\r\n]+)"\s*$', text, re.MULTILINE
                ) != ["read-only"]:
                    raise ValueError(f"{path.name}: candidate read-only sandbox missing")
                if re.search(r"^(?:service_tier|approval_policy)\s*=", text, re.MULTILINE) or 'developer_instructions = """' not in text:
                    raise ValueError(f"{path.name}: forbidden override or missing instructions")
        model_promotion.suite_sha256()
        return [Finding("PASS", "agent-model-config", "baseline and candidate agents match policy; read-only sandboxes enforced")]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return [Finding("FAIL", "agent-model-config", str(exc))]


def check_role_promotion(home: Path, *, codex: str = "codex") -> list[Finding]:
    policy = load_json(REPO_ROOT / "policies" / "model-routing.json")
    findings: list[Finding] = []
    for task_class, candidate in policy["role_promotion"]["candidates"].items():
        path = model_promotion.attestation_path(home, task_class)
        if not path.exists() and not path.is_symlink():
            findings.append(Finding("PASS", "role-promotion", f"{task_class}: baseline retained; no host attestation"))
            continue
        _, admitted, reason = model_promotion.host_attestation(home, policy, task_class, executable=codex)
        deployed = model_promotion.candidate_deployed(home, candidate)
        status = "PASS" if admitted and deployed else "WARN"
        detail = "candidate admitted and deployed" if status == "PASS" else f"baseline retained; {reason}" if not admitted else "baseline retained; candidate not deployed"
        findings.append(Finding(status, "role-promotion", f"{task_class}: {detail}"))
    return findings


def check_cli_compatibility(command: str = "codex") -> list[Finding]:
    """Report version preflight, never mistake it for a model/network canary."""
    executable = shutil.which(command)
    if executable is None:
        return [Finding("WARN", "codex-cli", "CLI not found; native model canary still required")]
    try:
        result = subprocess.run([executable, "--version"], capture_output=True,
                                text=True, timeout=10, check=True)
        match = re.search(r"\bcodex-cli (\d+)\.(\d+)\.(\d+)\b", result.stdout)
        if match is None:
            raise ValueError("unrecognized Codex CLI version output")
        found = tuple(int(part) for part in match.groups())
        baseline = load_json(REPO_ROOT / "policies" / "model-routing.json")["tested_cli_baseline"]
        tested = tuple(int(part) for part in baseline.split("."))
        if found < tested:
            return [Finding("WARN", "codex-cli", f"{match.group(0)} is older than tested baseline {baseline}; upgrade and run the same-model canary before claiming compatibility")]
        return [Finding("PASS", "codex-cli", f"{match.group(0)} meets tested version baseline; model availability still requires native canary")]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        return [Finding("WARN", "codex-cli", f"compatibility unverified: {exc}")]


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


def run(home: Path, cross_tool: bool, diff: bool, *, codex: str = "codex") -> tuple[list[Finding], dict[str, object]]:
    findings = check_public_harness() + check_json_documents() + check_repo_content() + check_outer_ignore(home) + check_agent_configuration() + check_cli_compatibility(codex) + check_role_promotion(home, codex=codex)
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
    parser.add_argument("--codex", default="codex", help="CLI executable for version preflight only")
    args = parser.parse_args()
    home = active_codex_home()
    findings, capabilities = run(home, args.cross_tool, args.diff, codex=args.codex)
    if args.json:
        print(json.dumps({"findings": [asdict(item) for item in findings], "host": capabilities}, ensure_ascii=False, indent=2))
    else:
        for item in findings:
            print(f"{item.status:4} {item.check}: {item.detail}")
        print(f"HOST {capabilities['os']} shell={capabilities['shell']} codex_home={home}")
    return 1 if any(item.status == "FAIL" for item in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
