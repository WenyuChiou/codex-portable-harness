"""Read-only completion checks. Evidence integrity is not execution authority.

Contracts are trusted task specifications. Event adapters are trusted host code,
never imported from a contract or selected by arbitrary Python paths.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path, PureWindowsPath
import stat


@dataclass(frozen=True)
class VerifiedEvent:
    task_id: str
    version: str
    environment: str
    phase: str
    result: str
    mechanism: str
    origin: str
    operation_id: str
    target: str


class EvidenceError(ValueError):
    pass


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise EvidenceError("duplicate_json_key")
        value[key] = item
    return value


def _json(raw):
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object)
    if not isinstance(value, dict):
        raise EvidenceError("expected_object")
    return value


def _digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _read(root, relative, digest, limit=64 * 1024 * 1024):
    if not isinstance(relative, str) or not relative or not _digest(digest):
        raise EvidenceError("invalid_evidence_reference")
    part = Path(relative)
    if part.is_absolute() or PureWindowsPath(relative).drive or "\\" in relative or any(p in (".", "..") for p in relative.split("/")):
        raise EvidenceError("evidence_path_outside_root")
    path = root / part
    for candidate in (root, *[root.joinpath(*part.parts[:n]) for n in range(1, len(part.parts) + 1)]):
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise EvidenceError("evidence_reparse_point")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise EvidenceError("evidence_size_limit")
    if hashlib.sha256(raw).hexdigest() != digest:
        raise EvidenceError("evidence_hash_mismatch")
    return path, raw


def _command(root, requirement):
    _, raw = _read(root, requirement["receipt"], requirement["sha256"])
    row = _json(raw)
    if row.get("owner") != "codex-test-run-v1" or row.get("state") != "finished" or row.get("wrapper_state") != "finished":
        raise EvidenceError("command_not_terminal")
    if row.get("command") != requirement["command"] or row.get("cwd") != requirement["cwd"]:
        raise EvidenceError("command_identity_mismatch")
    if not isinstance(requirement["command"], list) or not requirement["command"] or not all(isinstance(x, str) for x in requirement["command"]):
        raise EvidenceError("invalid_expected_command")
    if not isinstance(requirement["cwd"], str) or not requirement["cwd"]:
        raise EvidenceError("invalid_expected_cwd")
    for key in ("started", "finished"):
        if type(row.get(key)) not in (int, float) or not math.isfinite(row[key]):
            raise EvidenceError("invalid_command_time")
    if row["finished"] < row["started"]:
        raise EvidenceError("invalid_command_time")
    _read(root, str(Path(requirement["receipt"]).with_suffix(".log")).replace("\\", "/"), row.get("log_sha256"))
    if type(row.get("exit_code")) is not int or type(row.get("wrapper_exit_code")) is not int or "cleanup_error" not in row:
        raise EvidenceError("missing_command_status")
    if row["exit_code"] != 0 or row["wrapper_exit_code"] != 0 or row["cleanup_error"] is not None:
        return "failed", "command_or_cleanup_failed"
    return "passed", "terminal_command_and_log_verified"


def verify(contract, evidence_root, *, event_verifiers=None):
    """Check all required outcomes; this does not authorize work or retry it.

    Hash-pinned files establish byte integrity, not authorship. UI/human/receiver
    claims require a host-provided adapter that verifies the original source.
    CLI checks cannot supply or dynamically import such an adapter.
    """
    result = {"status": "unknown", "complete": False, "human_acceptance_verified": False,
              "execution_authorized": False, "checks": [], "issues": []}
    try:
        if not isinstance(contract, dict) or contract.get("schema_version") != 1:
            raise EvidenceError("invalid_contract")
        for name in ("task_id", "version", "environment"):
            if not isinstance(contract.get(name), str) or not contract[name].strip():
                raise EvidenceError("invalid_contract_identity")
        profile = contract.get("profile")
        if profile not in ("command", "artifact", "ui", "handoff"):
            raise EvidenceError("invalid_profile")
        requirements = contract.get("requirements")
        if not isinstance(requirements, list) or not requirements:
            raise EvidenceError("empty_requirements")
        if type(contract.get("require_human", False)) is not bool:
            raise EvidenceError("invalid_human_requirement")
        ids = [r.get("id") if isinstance(r, dict) else None for r in requirements]
        if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
            raise EvidenceError("invalid_requirement_identity")
        types = {r.get("type") for r in requirements}
        if types - {"command", "artifact", "event"}:
            raise EvidenceError("invalid_requirement_type")
        if "event" in types and (not isinstance(contract.get("operation_id"), str) or not contract["operation_id"].strip()):
            raise EvidenceError("operation_identity_missing")
        if profile in ("command", "artifact") and profile not in types:
            raise EvidenceError("profile_requirement_missing")
        phases = {r.get("phase") for r in requirements if r.get("type") == "event"}
        if profile == "ui" and not {"entry", "action", "outcome"} <= phases:
            raise EvidenceError("ui_boundaries_missing")
        if profile == "handoff" and not {"delivery", "receiver_ack"} <= phases:
            raise EvidenceError("handoff_boundaries_missing")
        human = [r for r in requirements if r.get("type") == "event" and r.get("origin") == "real_user"]
        if contract.get("require_human") and not human:
            raise EvidenceError("human_evidence_requirement_missing")
        root = Path(evidence_root).absolute()
        if not root.is_dir():
            raise EvidenceError("missing_evidence_root")
    except (EvidenceError, OSError, TypeError, ValueError):
        result["issues"].append("invalid_or_incomplete_contract")
        return result

    for r in requirements:
        try:
            kind = r["type"]
            if kind == "command":
                status, reason = _command(root, r)
            elif kind == "artifact":
                _read(root, r["path"], r["sha256"])
                status, reason = "passed", "artifact_bytes_verified"
            else:
                if not isinstance(r.get("target"), str) or not r["target"].strip():
                    raise EvidenceError("event_target_missing")
                _, raw = _read(root, r["path"], r["sha256"])
                adapter = (event_verifiers or {}).get(r.get("adapter"))
                if not callable(adapter):
                    raise EvidenceError("source_adapter_required")
                # The callback authenticates the source, not merely its JSON label.
                event = adapter(_json(raw), r, contract)
                if not isinstance(event, VerifiedEvent):
                    raise EvidenceError("source_adapter_did_not_verify")
                expected = (contract["task_id"], contract["version"], contract["environment"],
                            r["phase"], r["mechanism"], r["origin"], contract["operation_id"], r["target"])
                actual = (event.task_id, event.version, event.environment,
                          event.phase, event.mechanism, event.origin, event.operation_id, event.target)
                if actual != expected:
                    raise EvidenceError("verified_event_scope_mismatch")
                if profile == "ui" and r["phase"] in ("entry", "action") and event.mechanism not in ("click", "keyboard"):
                    raise EvidenceError("original_ui_action_required")
                if event.result not in ("passed", "failed"):
                    raise EvidenceError("event_outcome_unknown")
                status, reason = event.result, "source_adapter_verified"
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            status, reason = "unknown", str(exc) if isinstance(exc, EvidenceError) else "unreadable_or_invalid_evidence"
        except Exception:
            # A broken project adapter cannot promote a claim or crash the report.
            status, reason = "unknown", "source_adapter_error"
        result["checks"].append({"id": r["id"], "status": status, "reason": reason})
    result["complete"] = all(x["status"] == "passed" for x in result["checks"])
    result["status"] = "complete" if result["complete"] else ("failed" if any(x["status"] == "failed" for x in result["checks"]) else "unknown")
    result["human_acceptance_verified"] = bool(human) and result["complete"]
    return result


def main():
    parser = argparse.ArgumentParser(description="Verify a pinned task contract without executing work.")
    parser.add_argument("contract", type=Path)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--evidence-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        raw = args.contract.read_bytes()
        if not _digest(args.contract_sha256) or hashlib.sha256(raw).hexdigest() != args.contract_sha256:
            raise EvidenceError("contract_hash_mismatch")
        result = verify(_json(raw), args.evidence_root)
    except (OSError, ValueError) as exc:
        result = {"status": "unknown", "complete": False, "execution_authorized": False,
                  "issues": [str(exc) if isinstance(exc, EvidenceError) else "unreadable_contract"]}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
