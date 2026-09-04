from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from agent_collab_harness import evaluate_policy, validate_policy
from agent_collab_harness.errors import HarnessValidationError
from agent_collab_harness.io import (
    load_human_authorization_keys,
    canonical_sha256,
    load_json_object as load_json,
)
from agent_collab_harness.policy import fail_closed_decision

from common import REPO_ROOT, active_codex_home, ensure_within
from validators import validate_legacy_checkpoint


POLICY_PATH = REPO_ROOT / "policies" / "agent-budget.yaml"
LEGACY_TERMINAL_STATES = [
    "completed",
    "blocked",
    "budget_exhausted",
    "error",
    "cancelled",
]


def normalize_checkpoint(
    checkpoint: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
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
    # Preserve v1 stopping semantics; only explicit migration enables v2.
    if (
        public_checkpoint.get("schema_version") == 1
        and policy.get("schema_version") == 2
    ):
        policy = validate_policy(policy)["base_policy"]
    result = evaluate_policy(
        public_checkpoint,
        policy,
        authorization_keys=load_human_authorization_keys(
            os.environ.get("AGENT_COLLAB_HUMAN_KEYS_JSON")
        ),
    )
    return result.to_dict()


def main(*, test_home: Path | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate a TaskCheckpoint against agent budgets."
    )
    parser.add_argument("command", choices=("evaluate", "migrate", "advance"))
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("--policy", type=Path, default=POLICY_PATH)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--request-id")
    parser.add_argument("--expected-sha256")
    args = parser.parse_args()

    try:
        if args.command != "evaluate":
            from agent_collab_harness.goals import migrate_goal
            from agent_collab_harness.checkpoint_store import create_file, advance_file

            # No CLI override: injection exists only for isolated Python tests.
            home = active_codex_home() if test_home is None else test_home
            target = args.output if args.command == "migrate" else args.checkpoint
            if target is None:
                raise HarnessValidationError("migration requires --output")
            # Validate before the store can create directories or lock files.
            ensure_within(target, home)
            policy = load_json(args.policy)
            keys = load_human_authorization_keys(
                os.environ.get("AGENT_COLLAB_HUMAN_KEYS_JSON")
            )
            if args.command == "migrate":
                if args.metadata is None or args.output is None:
                    raise HarnessValidationError(
                        "migration requires --metadata and --output"
                    )
                metadata = load_json(args.metadata)
                if set(metadata) != {"goal_id", "next_step"}:
                    raise HarnessValidationError(
                        "metadata requires goal_id and next_step only"
                    )
                original = load_json(args.checkpoint)
                state = migrate_goal(
                    normalize_checkpoint(original, policy),
                    policy,
                    **metadata,
                    authorization_keys=keys,
                )
                state["source_sha256"] = canonical_sha256(original)
                create_file(args.output, state)
            else:
                if not args.request_id or not args.expected_sha256:
                    raise HarnessValidationError(
                        "advance requires request id and expected SHA-256"
                    )
                state = advance_file(
                    args.checkpoint,
                    policy,
                    args.request_id,
                    args.expected_sha256,
                    authorization_keys=keys,
                )
            print(
                json.dumps(
                    {
                        "ok": True,
                        "schema_version": 2,
                        "task_id": state["task_id"],
                        "slice_id": state["slice_id"],
                        "totals": state["totals"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        result = evaluate(load_json(args.checkpoint), load_json(args.policy))
    except (
        HarnessValidationError,
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
    ) as exc:
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
