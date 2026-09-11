from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from common import REPO_ROOT, load_json

POLICY_PATH = REPO_ROOT / "policies" / "model-routing.json"
HASH = re.compile(r"^[a-f0-9]{64}$")
ATTESTATION_FIELDS = {
    "host_id",
    "issued_at",
    "routing_policy_sha256",
    "suite_sha256",
    "runtime_sha256",
    "paired_runs",
    "quality_checks",
    "cost_improvement_percent",
    "comparable_token_improvement_percent",
    "wall_time_regression_percent",
}


def canonical_policy_sha256(policy: dict[str, Any]) -> str:
    payload = json.dumps(
        policy,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _issued_at(value: Any, now: datetime, lifetime_days: int) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        issued = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    age = now - issued
    return (
        issued.tzinfo is not None
        and age.total_seconds() >= 0
        and age.total_seconds() <= lifetime_days * 86400
    )


def valid_attestation(
    value: dict[str, Any] | None,
    policy: dict[str, Any],
    *,
    host_id: str | None,
    current_policy_sha256: str | None,
    current_suite_sha256: str | None,
    current_runtime_sha256: str | None,
    now: datetime | None = None,
) -> bool:
    if not isinstance(value, dict) or set(value) != ATTESTATION_FIELDS:
        return False
    fingerprints = (current_policy_sha256, current_suite_sha256, current_runtime_sha256)
    if (
        not host_id
        or not HASH.fullmatch(host_id)
        or not all(
            isinstance(item, str) and HASH.fullmatch(item) for item in fingerprints
        )
    ):
        return False
    if current_policy_sha256 != canonical_policy_sha256(policy):
        return False
    if value["host_id"] != host_id or any(
        value[key] != actual
        for key, actual in zip(
            ("routing_policy_sha256", "suite_sha256", "runtime_sha256"), fingerprints
        )
    ):
        return False
    minimum = policy["minimum_attestation"]
    checks = value["quality_checks"]
    checks_valid = (
        isinstance(checks, list)
        and len(checks) == minimum["quality_checks"]
        and all(
            isinstance(item, dict)
            and set(item) == {"id", "passed"}
            and isinstance(item["id"], str)
            and item["id"].strip()
            and item["passed"] is True
            for item in checks
        )
        and len({item["id"] for item in checks}) == len(checks)
    )
    paired = value["paired_runs"]
    gains = (
        value["cost_improvement_percent"],
        value["comparable_token_improvement_percent"],
    )
    finite_gains = [item for item in gains if _finite_number(item)]
    gains_valid = all(item is None or _finite_number(item) for item in gains)
    wall = value["wall_time_regression_percent"]
    return bool(
        isinstance(paired, int)
        and not isinstance(paired, bool)
        and paired >= minimum["paired_runs"]
        and checks_valid
        and gains_valid
        and finite_gains
        and max(finite_gains)
        >= minimum["minimum_cost_or_comparable_token_improvement_percent"]
        and _finite_number(wall)
        and wall <= minimum["maximum_wall_time_regression_percent"]
        and _issued_at(
            value["issued_at"],
            now or datetime.now(timezone.utc),
            minimum["lifetime_days"],
        )
    )


def route_execution(
    category: str,
    *,
    mode: str = "default",
    task_effect: str | None = None,
    available_models: Iterable[str] = (),
    experimental: bool = False,
    permission_denied: bool = False,
    parent_model: str | None = None,
    escalation_reason: str | None = None,
    evidence_refs: Iterable[str] = (),
    computer_use_available: bool = False,
    host_id: str | None = None,
    attestation: dict[str, Any] | None = None,
    current_policy_sha256: str | None = None,
    current_suite_sha256: str | None = None,
    current_runtime_sha256: str | None = None,
    policy: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    policy = policy or load_json(POLICY_PATH)
    available = sorted(set(available_models))
    # Preserve legacy callers: mechanical/implementation may write unless a
    # mechanical task is explicitly scoped to read-only output.
    effect = task_effect if task_effect is not None else {
        "mechanical": "write", "implementation": "write",
        "exploration": "read-only", "review": "read-only",
    }.get(category, "unknown")
    direct = {
        "model": None,
        "effort": None,
        "role": "parent",
        "service_tier": "unchanged",
        "available": available,
        "task_effect": effect,
        "read_only_required": effect == "read-only",
    }
    if permission_denied:
        return {
            **direct,
            "action": "blocked",
            "reason": "permission denied; model fallback and parent bypass are prohibited",
        }
    if task_effect not in (None, "read-only", "write"):
        return {**direct, "action": "parent_decides", "reason": "task effect must be read-only or write"}
    if mode.lower() == "plan" and (effect == "write" or category == "implementation"):
        return {
            **direct,
            "action": "plan_only",
            "reason": "plan mode prohibits write-task dispatch",
        }
    if (category in {"exploration", "review"} and effect == "write") or (
        category == "implementation" and effect == "read-only"
    ):
        return {**direct, "action": "parent_decides", "reason": "task class conflicts with its effect; reclassify the bounded task"}
    if escalation_reason is not None:
        # Quality escalation is an explicit routing preference, not evidence of
        # economical rollout, model superiority, or permission to execute.
        escalation = policy.get("quality_escalation", {})
        evidence = list(evidence_refs)
        if category in {"trivial", "governance", "security"}:
            return {**direct, "action": "parent", "reason": f"{category} ownership remains with the parent; use a bounded review task for independent advice"}
        allowed = escalation.get("reasons", {}).get(escalation_reason, ())
        missing = None
        if escalation.get("enabled") is not True:
            missing = "quality escalation is not enabled"
        elif category not in allowed:
            missing = "escalation reason does not permit this task class"
        elif not isinstance(parent_model, str) or not parent_model.strip():
            missing = "quality escalation requires the actual parent model"
        elif not evidence or any(not isinstance(ref, str) or not ref.strip() for ref in evidence):
            missing = "quality escalation requires bounded evidence references"
        elif escalation_reason == "complex_computer_use" and computer_use_available is not True:
            missing = "computer-use tools and the intended surface must be available to the child"
        if missing:
            return {**direct, "action": "parent_decides", "reason": missing}
        if parent_model == escalation["model"]:
            return {**direct, "action": "parent", "reason": "parent already uses the escalation model; independent review may still use another context"}
        if escalation["model"] not in available:
            return {**direct, "action": "parent_decides", "reason": f"required model {escalation['model']} is unavailable; no silent fallback"}
        return {
            **direct,
            "model": escalation["model"],
            "effort": escalation["effort"],
            "role": escalation["review_role"] if category == "review" else escalation["role"],
            "action": "evaluation_only" if experimental else "dispatch",
            "reason": "explicit bounded quality escalation; host permissions and checkpoint gates still apply",
            "escalation_reason": escalation_reason,
            "evidence_refs": evidence,
            "routing_only_not_execution_authorization": True,
        }
    if category in policy["direct_categories"] or category not in policy["routes"]:
        return {
            **direct,
            "action": "parent",
            "reason": f"{category} work remains with the parent",
        }
    proof = valid_attestation(
        attestation,
        policy,
        host_id=host_id,
        current_policy_sha256=current_policy_sha256,
        current_suite_sha256=current_suite_sha256,
        current_runtime_sha256=current_runtime_sha256,
        now=now,
    )
    # Only explicit False enables preference-based dispatch without benchmark
    # admission. Older policies retain their proof requirement by default.
    require_proof = policy.get("economical_routing_require_attestation", True) is not False
    rollout_enabled = policy["economical_routing_default_enabled"] is True
    if not experimental and (not rollout_enabled or (require_proof and not proof)):
        reason = (
            "stable economical routing rollout is disabled"
            if not rollout_enabled
            else "valid current host-local benchmark proof is missing"
        )
        return {**direct, "action": "parent", "reason": reason}
    selected = policy["routes"][category]
    if selected["model"] not in available:
        return {
            **direct,
            "action": "parent_decides",
            "reason": f"required model {selected['model']} is unavailable; no silent fallback",
        }
    action = "evaluation_only" if experimental else "dispatch"
    if experimental:
        reason = "explicit experimental evaluation only"
    elif require_proof:
        reason = "enabled rollout with validated current host-local benchmark proof"
    else:
        reason = "automatic task-class routing per configured preference; benchmark admission is not required"
    return {
        **selected,
        "task_effect": effect,
        "read_only_required": effect == "read-only",
        "service_tier": "unchanged",
        "available": available,
        "action": action,
        "reason": reason,
        "attestation_valid": proof,
        "routing_only_not_execution_authorization": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Select a bounded portable execution route."
    )
    choices = [
        "mechanical",
        "exploration",
        "implementation",
        "trivial",
        "governance",
        "security",
        "review",
    ]
    parser.add_argument("command_or_category", nargs="?")
    parser.add_argument("--task-class", dest="task_class", choices=choices)
    parser.add_argument("--mode", default="default")
    parser.add_argument("--task-effect", choices=("read-only", "write"),
                        help="Scope task effects separately from model class; read-only forbids file, tool and external mutations.")
    parser.add_argument("--available-model", action="append", default=[])
    parser.add_argument(
        "--experimental",
        action="store_true",
        help="Select an evaluation-only route; never authorizes dispatch.",
    )
    parser.add_argument("--permission-denied", action="store_true")
    parser.add_argument("--parent-model")
    parser.add_argument("--escalation-reason")
    parser.add_argument("--evidence-ref", action="append", default=[])
    parser.add_argument("--computer-use-available", action="store_true",
                        help="Assert the child has the required tools and intended surface; does not grant access.")
    parser.add_argument("--attestation", type=Path)
    parser.add_argument("--host-id")
    parser.add_argument("--current-policy-sha256")
    parser.add_argument("--current-suite-sha256")
    parser.add_argument("--current-runtime-sha256")
    args = parser.parse_args()
    positional = args.command_or_category
    if positional == "select":
        category = args.task_class
    else:
        category = args.task_class or positional
    if (
        not category
        or category not in choices
        or (
            positional not in (None, "select")
            and args.task_class
            and args.task_class != positional
        )
    ):
        parser.error("provide one task class (positional or --task-class)")
    attestation = load_json(args.attestation) if args.attestation else None
    result = route_execution(
        category,
        mode=args.mode,
        task_effect=args.task_effect,
        available_models=args.available_model,
        experimental=args.experimental,
        permission_denied=args.permission_denied,
        parent_model=args.parent_model,
        escalation_reason=args.escalation_reason,
        evidence_refs=args.evidence_ref,
        computer_use_available=args.computer_use_available,
        attestation=attestation,
        host_id=args.host_id,
        current_policy_sha256=args.current_policy_sha256,
        current_suite_sha256=args.current_suite_sha256,
        current_runtime_sha256=args.current_runtime_sha256,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
