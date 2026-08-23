from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import REPO_ROOT, load_json
from validators import validate_checkpoint


POLICY_PATH = REPO_ROOT / "policies" / "agent-budget.yaml"

METRIC_LIMITS = {
    "cycles": "max_plan_act_reflect_cycles",
    "tool_calls": "max_tool_calls",
    "elapsed_seconds": "max_wall_time_seconds",
    "transcript_bytes": "max_transcript_bytes",
    "same_failure_retries": "max_same_failure_retries",
    "no_evidence_cycles": "max_no_evidence_cycles",
}

CONTEXT_LIMITS = {
    "task_packet_tokens": "task_packet_tokens",
    "parent_summary_tokens": "parent_summary_tokens",
    "memory_digest_tokens": "memory_digest_tokens",
    "child_result_words": "child_result_words",
}


def effective_limits(checkpoint: dict[str, Any], policy: dict[str, Any]) -> dict[str, int]:
    limits = dict(policy["limits"])
    override = checkpoint.get("override")
    if override:
        reason = override.get("reason", "").strip()
        if not reason:
            raise ValueError("budget override requires a non-empty reason")
        for key, value in override.get("limits", {}).items():
            if key not in limits or not isinstance(value, int) or value < limits[key]:
                raise ValueError(f"invalid budget override: {key}={value!r}")
            limits[key] = value
    return limits


def evaluate(checkpoint: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    terminal_states = policy.get("terminal_states")
    if not isinstance(terminal_states, list):
        raise ValueError("policy.terminal_states must be an array")
    validate_checkpoint(checkpoint, terminal_states)
    metrics = checkpoint.get("metrics")
    assert isinstance(metrics, dict)
    limits = effective_limits(checkpoint, policy)
    reasons: list[str] = []
    if checkpoint["status"] != "running":
        reasons.append(f"task status is terminal: {checkpoint['status']}")
    for metric, limit_name in METRIC_LIMITS.items():
        value = metrics.get(metric)
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"checkpoint.metrics.{metric} must be a non-negative integer")
        if value >= limits[limit_name]:
            reasons.append(f"{metric}={value} reached {limit_name}={limits[limit_name]}")

    context = policy["context"]
    for metric, limit_name in CONTEXT_LIMITS.items():
        value = metrics.get(metric)
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"checkpoint.metrics.{metric} must be a non-negative integer")
        if value > context[limit_name]:
            reasons.append(f"{metric}={value} exceeded {limit_name}={context[limit_name]}")

    children_spawned = metrics.get("children_spawned")
    active_children = metrics.get("active_children")
    if not isinstance(children_spawned, int) or children_spawned < 0:
        raise ValueError("checkpoint.metrics.children_spawned must be a non-negative integer")
    if not isinstance(active_children, int) or active_children < 0:
        raise ValueError("checkpoint.metrics.active_children must be a non-negative integer")
    if children_spawned > limits["max_children_per_parent"]:
        reasons.append(f"children_spawned={children_spawned} exceeded max_children_per_parent={limits['max_children_per_parent']}")
    if active_children > limits["max_concurrency"]:
        reasons.append(f"active_children={active_children} exceeded max_concurrency={limits['max_concurrency']}")

    decision = "stop" if reasons else "continue"
    if decision == "continue":
        current = metrics["transcript_bytes"]
        previous = metrics.get("last_checkpoint_bytes", 0)
        threshold = limits["checkpoint_transcript_bytes"]
        if current - previous >= threshold:
            decision = "checkpoint"
            reasons.append(f"transcript grew {current - previous} bytes since checkpoint")

    spawn_allowed = (
        decision == "continue"
        and children_spawned < limits["max_children_per_parent"]
        and active_children < limits["max_concurrency"]
    )
    return {
        "decision": decision,
        "spawn_allowed": spawn_allowed,
        "reasons": reasons,
        "effective_limits": limits,
        "context_limits": context,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate a TaskCheckpoint against agent budgets.")
    parser.add_argument("command", choices=("evaluate",))
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("--policy", type=Path, default=POLICY_PATH)
    args = parser.parse_args()

    result = evaluate(load_json(args.checkpoint), load_json(args.policy))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 2 if result["decision"] == "stop" else 0


if __name__ == "__main__":
    raise SystemExit(main())
