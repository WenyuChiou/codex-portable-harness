from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from agent_collab_harness import evaluate_policy, validate_policy
from agent_collab_harness.errors import HarnessValidationError
from agent_collab_harness.io import load_human_authorization_keys
from agent_collab_harness.policy import fail_closed_decision

from common import REPO_ROOT, load_json
from validators import validate_legacy_checkpoint


POLICY_PATH = REPO_ROOT / "policies" / "agent-budget.yaml"
LEGACY_TERMINAL_STATES = [
    "completed",
    "blocked",
    "budget_exhausted",
    "error",
    "cancelled",
]

def normalize_checkpoint(checkpoint: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """Convert the private v1 checkpoint shape to the public v1 contract."""

    if "metrics" not in checkpoint:
        return checkpoint
    validate_policy(policy)
    validate_legacy_checkpoint(checkpoint, LEGACY_TERMINAL_STATES)
    if checkpoint.get("override") is not None:
        raise HarnessValidationError(
            "legacy unsigned overrides are unsupported; create a signed public override before exhaustion"
        )
    metrics = checkpoint["metrics"]
    return {
        "schema_version": 1,
        "task_id": checkpoint["task_id"],
        "parent_id": checkpoint.get("parent_id"),
        "status": checkpoint["status"],
        "cycle": metrics["cycles"],
        "tool_calls": metrics["tool_calls"],
        "elapsed_seconds": metrics["elapsed_seconds"],
        "transcript_bytes": metrics["transcript_bytes"],
        "same_failure_retries": metrics["same_failure_retries"],
        "no_evidence_cycles": metrics["no_evidence_cycles"],
        "children_spawned": metrics["children_spawned"],
        "active_children": metrics["active_children"],
        "task_packet_tokens": metrics["task_packet_tokens"],
        "parent_summary_tokens": metrics["parent_summary_tokens"],
        "memory_digest_tokens": metrics["memory_digest_tokens"],
        "child_result_words": metrics["child_result_words"],
        "last_checkpoint_transcript_bytes": metrics.get("last_checkpoint_bytes", 0),
        "evidence_refs": checkpoint["evidence_refs"],
        "decisions": [],
        "overrides": [],
        "stop_reason": checkpoint.get("stop_reason"),
        "updated_at": "1970-01-01T00:00:00Z",
    }


def evaluate(checkpoint: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """Delegate all policy decisions to the released public harness."""

    public_checkpoint = normalize_checkpoint(checkpoint, policy)
    result = evaluate_policy(
        public_checkpoint,
        policy,
        authorization_keys=load_human_authorization_keys(
            os.environ.get("AGENT_COLLAB_HUMAN_KEYS_JSON")
        ),
    )
    return result.to_dict()


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate a TaskCheckpoint against agent budgets.")
    parser.add_argument("command", choices=("evaluate",))
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("--policy", type=Path, default=POLICY_PATH)
    args = parser.parse_args()

    try:
        result = evaluate(load_json(args.checkpoint), load_json(args.policy))
    except (HarnessValidationError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        result = fail_closed_decision(
            policy_id=args.policy.stem.strip() or "unreadable-policy",
            reason=str(exc),
        ).to_dict()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if result["decision"] == "stop":
        return 4
    if result["decision"] == "checkpoint":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
