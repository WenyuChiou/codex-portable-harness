"""Host-local, fail-closed admission for candidate Codex agent roles."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from common import REPO_ROOT, ensure_within, sha256

HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
AGENT_NAME = re.compile(r"^portable-[a-z0-9-]+$")
ATTESTATION_FIELDS = {
    "schema_version", "task_class", "host_id", "issued_at", "os",
    "python_version", "codex_version", "service_tier", "policy_sha256",
    "suite_sha256", "baseline_model", "baseline_effort", "candidate_model",
    "candidate_effort", "paired_runs", "quality_checks", "control_failures",
    "candidate_failures", "control_timeouts", "candidate_timeouts",
    "control_retries", "candidate_retries", "sandbox_violations",
    "median_wall_regression_percent", "median_cost_proxy_regression_percent",
    "source_unchanged", "evidence_name", "evidence_sha256",
}
EVIDENCE_NAME = re.compile(r"^(?:mechanical|exploration|implementation)-\d{8}T\d{12}Z\.json$")
SUITE_FILES = (
    REPO_ROOT / "scripts" / "benchmark_role_promotion.py",
    REPO_ROOT / "scripts" / "benchmark_autonomy.py",
    REPO_ROOT / "scripts" / "model_promotion.py",
    REPO_ROOT / "scripts" / "common.py",
    REPO_ROOT / "fixtures" / "role_promotion" / "cases.json",
)


def canonical_policy_sha256(policy: dict[str, Any]) -> str:
    encoded = json.dumps(
        policy, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def suite_sha256() -> str:
    digest = hashlib.sha256()
    for path in SUITE_FILES:
        digest.update(path.relative_to(REPO_ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _safe_local_file(path: Path, home: Path) -> bool:
    try:
        ensure_within(path, home)
    except ValueError:
        return False
    current = path
    while current != home:
        if current.is_symlink():
            return False
        current = current.parent
    return not home.is_symlink()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def strict_local_json(path: Path, home: Path) -> dict[str, Any] | None:
    if not _safe_local_file(path, home) or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_strict_object)
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def host_id_path(home: Path) -> Path:
    return home / "harness-attestations" / "host-id.txt"


def attestation_path(home: Path, task_class: str) -> Path:
    if task_class not in {"mechanical", "exploration", "implementation"}:
        raise ValueError("unknown promotion task class")
    return home / "harness-attestations" / f"model-role-{task_class}.json"


def read_host_salt(home: Path) -> str | None:
    path = host_id_path(home)
    if not _safe_local_file(path, home) or not path.is_file():
        return None
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    return value if HEX_SHA256.fullmatch(value) else None


def _machine_identity() -> str | None:
    """Read an OS identity outside transferable CODEX_HOME; never store it raw."""
    system = platform.system().lower()
    if system == "windows":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return value.strip().lower() if isinstance(value, str) and value.strip() else None
        except (ImportError, OSError):
            return None
    if system == "darwin":
        try:
            result = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                    capture_output=True, text=True, timeout=5, shell=False)
        except (OSError, subprocess.SubprocessError):
            return None
        found = re.search(r'"IOPlatformUUID"\s*=\s*"([^"\r\n]+)"', result.stdout)
        return found.group(1).strip().lower() if result.returncode == 0 and found else None
    if system == "linux":
        for path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
            try:
                value = path.read_text(encoding="ascii").strip().lower()
            except (OSError, UnicodeError):
                continue
            if re.fullmatch(r"[0-9a-f]{32}", value):
                return value
    return None


def host_id(home: Path) -> str | None:
    salt, machine = read_host_salt(home), _machine_identity()
    if salt is None or machine is None:
        return None
    return hashlib.sha256(f"codex-portable-harness:{salt}:{machine}".encode("utf-8")).hexdigest()


def codex_version(executable: str = "codex") -> str | None:
    resolved = shutil.which(executable)
    if resolved is None:
        return None
    try:
        result = subprocess.run(
            [resolved, "--version"], capture_output=True, text=True,
            timeout=10, shell=False, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.fullmatch(r"codex-cli (\d+\.\d+\.\d+)\s*", result.stdout)
    return match.group(1) if result.returncode == 0 and match else None


def service_tier(home: Path) -> str | None:
    path = home / "config.toml"
    if not _safe_local_file(path, home):
        return None
    if not path.is_file():
        return "unconfigured"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    # Only the top-level setting controls this run; profile tables may contain
    # other tiers without changing the active session.
    top_level = re.split(r"^\s*\[", text, maxsplit=1, flags=re.MULTILINE)[0]
    matches = re.findall(r'^service_tier\s*=\s*"([^"\r\n]+)"\s*$', top_level, re.MULTILINE)
    if len(matches) > 1:
        return None
    selected = re.findall(r'^profile\s*=\s*"([^"\r\n]+)"\s*$', top_level, re.MULTILINE)
    if len(selected) > 1:
        return None
    if selected:
        section = re.search(r'^\[profiles\.' + re.escape(selected[0]) + r'\]\s*$(.*?)(?=^\s*\[|\Z)',
                            text, re.MULTILINE | re.DOTALL)
        if section is None:
            return None
        profile_tiers = re.findall(r'^service_tier\s*=\s*"([^"\r\n]+)"\s*$', section.group(1), re.MULTILINE)
        if len(profile_tiers) > 1:
            return None
        if profile_tiers:
            return profile_tiers[0]
    return matches[0] if matches else "unconfigured"


def current_environment(home: Path, *, executable: str = "codex") -> dict[str, str] | None:
    host = host_id(home)
    cli = codex_version(executable)
    tier = service_tier(home)
    if host is None or cli is None or tier is None:
        return None
    system = platform.system().lower()
    return {
        "host_id": host,
        "os": "macos" if system == "darwin" else system,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "codex_version": cli,
        "service_tier": tier,
    }


def candidate_agent_source(role: str) -> Path:
    if not isinstance(role, str) or not AGENT_NAME.fullmatch(role):
        raise ValueError("invalid candidate agent role")
    return REPO_ROOT / "agents" / "candidates" / f"{role}.toml"


def candidate_roles(candidate: dict[str, Any]) -> tuple[str, ...]:
    roles = [candidate["role"]]
    if "read_only_role" in candidate:
        roles.append(candidate["read_only_role"])
    if len(set(roles)) != len(roles):
        raise ValueError("duplicate candidate roles")
    for role in roles:
        candidate_agent_source(role)
    return tuple(roles)


def candidate_deployed(home: Path, candidate: dict[str, Any]) -> bool:
    try:
        for role in candidate_roles(candidate):
            source = candidate_agent_source(role)
            target = home / "agents" / f"{role}.toml"
            if not source.is_file() or not _safe_local_file(target, home):
                return False
            if not target.is_file() or sha256(source) != sha256(target):
                return False
    except (OSError, ValueError):
        return False
    return True


def _fresh_utc(value: Any, lifetime_days: int, now: datetime) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    age = (now - parsed).total_seconds()
    return 0 <= age <= lifetime_days * 86400


def valid_attestation(
    value: dict[str, Any] | None, policy: dict[str, Any], task_class: str,
    environment: dict[str, str] | None, *, now: datetime | None = None,
    suite_hash: str | None = None,
) -> tuple[bool, str]:
    settings = policy.get("role_promotion", {})
    candidate = settings.get("candidates", {}).get(task_class)
    baseline = policy.get("routes", {}).get(task_class)
    if not isinstance(candidate, dict) or not isinstance(baseline, dict):
        return False, "no approved candidate for task class"
    if value is None:
        return False, "host-local promotion attestation is missing or invalid"
    if not isinstance(value, dict) or set(value) != ATTESTATION_FIELDS or value.get("schema_version") != 1:
        return False, "promotion attestation schema mismatch"
    if environment is None or any(value.get(key) != item for key, item in environment.items()):
        return False, "promotion host or runtime mismatch"
    expected = {
        "task_class": task_class,
        "policy_sha256": canonical_policy_sha256(policy),
        "suite_sha256": suite_hash or suite_sha256(),
        "baseline_model": baseline["model"],
        "baseline_effort": baseline["effort"],
        "candidate_model": candidate["model"],
        "candidate_effort": candidate["effort"],
    }
    if any(value.get(key) != item for key, item in expected.items()):
        return False, "promotion policy, suite, or model mismatch"
    if (not isinstance(value.get("evidence_name"), str) or not EVIDENCE_NAME.fullmatch(value["evidence_name"])
        or not isinstance(value.get("evidence_sha256"), str)
        or not HEX_SHA256.fullmatch(value["evidence_sha256"])):
        return False, "promotion raw evidence reference is invalid"
    clock = now or datetime.now(timezone.utc)
    lifetime = settings["lifetime_days"]
    if not _fresh_utc(value.get("issued_at"), lifetime, clock):
        return False, "promotion attestation expired or future-dated"
    try:
        pricing_date = date.fromisoformat(settings["price_checked_at"])
    except (TypeError, ValueError):
        return False, "promotion price snapshot is invalid"
    pricing_age = (clock.date() - pricing_date).days
    if pricing_age < 0 or pricing_age > lifetime:
        return False, "promotion price snapshot is stale"
    pairs = value["paired_runs"]
    checks = value["quality_checks"]
    if (
        type(pairs) is not int or pairs < settings["minimum_paired_runs"]
        or not isinstance(checks, list) or len(checks) != pairs
        or any(not isinstance(check, dict) or set(check) != {"id", "passed"}
               or not isinstance(check["id"], str) or not check["id"]
               or check["passed"] is not True for check in checks)
        or len({check["id"] for check in checks}) != pairs
        or value["source_unchanged"] is not True
    ):
        return False, "promotion quality or source-integrity gate failed"
    for key in ("control_failures", "candidate_failures", "control_timeouts",
                "candidate_timeouts", "control_retries", "candidate_retries",
                "sandbox_violations"):
        if type(value[key]) is not int or value[key] < 0:
            return False, "promotion failure counters are invalid"
    if (value["candidate_failures"] != 0
        or value["candidate_failures"] > value["control_failures"]
        or value["candidate_timeouts"] > value["control_timeouts"]
        or value["candidate_retries"] > value["control_retries"]
        or value["sandbox_violations"] != 0):
        return False, "promotion failures, retries, or violations increased"
    for key, limit in (("median_wall_regression_percent", settings["maximum_wall_regression_percent"]),
                       ("median_cost_proxy_regression_percent", settings["maximum_cost_proxy_regression_percent"])):
        metric = value[key]
        if type(metric) not in (int, float) or not math.isfinite(metric) or not (-100 <= metric <= limit):
            return False, f"promotion {key} exceeds limit or is unavailable"
    return True, "current host-local promotion evidence passed"


def host_attestation(
    home: Path, policy: dict[str, Any], task_class: str,
    *, executable: str = "codex",
) -> tuple[dict[str, Any] | None, bool, str]:
    path = attestation_path(home, task_class)
    value = strict_local_json(path, home)
    if value is None:
        return None, False, "host-local promotion attestation is missing or invalid"
    environment = current_environment(home, executable=executable)
    try:
        valid, reason = valid_attestation(value, policy, task_class, environment)
    except (OSError, KeyError, TypeError, ValueError):
        return value, False, "promotion policy or suite is invalid"
    if not valid:
        return value, False, reason
    evidence_path = home / "experiments" / "model-role-promotion" / value["evidence_name"]
    raw = strict_local_json(evidence_path, home)
    if raw is None:
        return value, False, "promotion raw evidence is missing or invalid"
    try:
        if sha256(evidence_path) != value["evidence_sha256"]:
            return value, False, "promotion raw evidence hash mismatch"
        from benchmark_role_promotion import evaluate
        passed, _, metrics = evaluate(raw, policy)
        if (not passed or raw.get("task_class") != task_class
            or raw.get("policy_sha256") != value["policy_sha256"]
            or raw.get("suite_sha256") != value["suite_sha256"]
            or raw.get("complete") is not True or raw.get("admitted") is not True
            or {check["id"] for check in value["quality_checks"]}
               != {run["pair"] for run in raw["runs"]}
            or any(value.get(key) != item for key, item in metrics.items())):
            return value, False, "promotion raw evidence does not support the attestation"
    except (OSError, KeyError, TypeError, ValueError):
        return value, False, "promotion raw evidence cannot be verified"
    return value, valid, reason
