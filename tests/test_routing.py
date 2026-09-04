from __future__ import annotations

import json
import math
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import route_execution  # noqa: E402
from common import load_json  # noqa: E402

AVAILABLE = ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]
NOW = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
HOST = "1" * 64
SUITE = "2" * 64
RUNTIME = "3" * 64


def enabled_policy():
    value = load_json(ROOT / "policies" / "model-routing.json")
    value["economical_routing_default_enabled"] = True
    return value


def attestation(policy=None, **changes):
    policy = policy or enabled_policy()
    value = {
        "host_id": HOST,
        "issued_at": "2026-09-04T11:00:00Z",
        "routing_policy_sha256": route_execution.canonical_policy_sha256(policy),
        "suite_sha256": SUITE,
        "runtime_sha256": RUNTIME,
        "paired_runs": 8,
        "quality_checks": [
            {"id": f"quality-{index}", "passed": True} for index in range(8)
        ],
        "cost_improvement_percent": 15,
        "comparable_token_improvement_percent": 15,
        "wall_time_regression_percent": 15,
    }
    value.update(changes)
    return value


def routed(category="mechanical", *, policy=None, evidence=None, **changes):
    policy = policy or enabled_policy()
    available = changes.pop("available_models", AVAILABLE)
    return route_execution.route_execution(
        category,
        policy=policy,
        attestation=evidence or attestation(policy),
        host_id=HOST,
        current_policy_sha256=route_execution.canonical_policy_sha256(policy),
        current_suite_sha256=SUITE,
        current_runtime_sha256=RUNTIME,
        available_models=available,
        now=NOW,
        **changes,
    )


class RoutingTests(unittest.TestCase):
    def test_canonical_model_map_and_stable_dispatch(self):
        expected = {
            "mechanical": ("gpt-5.6-luna", "low", "portable-mechanical"),
            "exploration": ("gpt-5.6-terra", "medium", "portable-explorer"),
            "implementation": ("gpt-5.6-sol", "medium", "portable-worker"),
        }
        for category, fields in expected.items():
            result = routed(category)
            self.assertEqual(
                (result["model"], result["effort"], result["role"]), fields
            )
            self.assertEqual(result["action"], "dispatch")
            self.assertEqual(result["service_tier"], "unchanged")

    def test_rollout_flag_defaults_to_parent_even_with_valid_proof(self):
        policy = load_json(ROOT / "policies" / "model-routing.json")
        result = routed(policy=policy, evidence=attestation(policy))
        self.assertEqual((result["action"], result["role"]), ("parent", "parent"))
        self.assertIn("rollout", result["reason"])

    def test_missing_proof_routes_direct_to_parent_when_rollout_enabled(self):
        policy = enabled_policy()
        result = route_execution.route_execution(
            "mechanical", policy=policy, available_models=AVAILABLE
        )
        self.assertEqual(result["action"], "parent")
        self.assertIn("proof", result["reason"])

    def test_experimental_is_evaluation_only_without_proof(self):
        result = route_execution.route_execution(
            "mechanical", experimental=True, available_models=AVAILABLE
        )
        self.assertEqual(result["action"], "evaluation_only")
        self.assertIn("evaluation only", result["reason"])

    def test_plan_write_task_is_plan_only(self):
        result = routed("implementation", mode="plan")
        self.assertEqual(result["action"], "plan_only")

    def test_permission_denial_is_blocked_not_parent_bypass(self):
        result = routed("exploration", permission_denied=True)
        self.assertEqual(result["action"], "blocked")
        self.assertIn("bypass", result["reason"])

    def test_unavailable_model_is_explicit_parent_decision(self):
        result = routed("implementation", available_models=["gpt-5.6-luna"])
        self.assertEqual(result["action"], "parent_decides")
        self.assertEqual(result["available"], ["gpt-5.6-luna"])

    def test_sensitive_and_reviewer_work_inherits_parent(self):
        policy = load_json(ROOT / "policies" / "model-routing.json")
        self.assertEqual(policy["independent_reviewer"], "inherit-parent")
        for category in ("trivial", "governance", "security", "review"):
            self.assertEqual(
                route_execution.route_execution(category, experimental=True)["action"],
                "parent",
            )

    def test_attestation_requires_current_actual_fingerprints(self):
        policy = enabled_policy()
        cases = [
            {"host_id": "workstation.example.com"},
            {"routing_policy_sha256": "4" * 64},
            {"suite_sha256": "4" * 64},
            {"runtime_sha256": "4" * 64},
        ]
        for change in cases:
            self.assertFalse(
                route_execution.valid_attestation(
                    attestation(policy, **change),
                    policy,
                    host_id=HOST,
                    current_policy_sha256=route_execution.canonical_policy_sha256(
                        policy
                    ),
                    current_suite_sha256=SUITE,
                    current_runtime_sha256=RUNTIME,
                    now=NOW,
                )
            )

    def test_attestation_rejects_stale_future_and_non_utc_dates(self):
        policy = enabled_policy()
        for issued in (
            "2026-08-05T11:59:59Z",
            "2026-09-04T12:00:01Z",
            "2026-09-04T11:00:00+00:00",
        ):
            self.assertFalse(
                route_execution.valid_attestation(
                    attestation(policy, issued_at=issued),
                    policy,
                    host_id=HOST,
                    current_policy_sha256=route_execution.canonical_policy_sha256(
                        policy
                    ),
                    current_suite_sha256=SUITE,
                    current_runtime_sha256=RUNTIME,
                    now=NOW,
                )
            )

    def test_attestation_rejects_bad_checks_thresholds_and_bool_numeric(self):
        policy = enabled_policy()
        cases = [
            {"paired_runs": 7},
            {"paired_runs": True},
            {"quality_checks": [{"id": str(i), "passed": True} for i in range(7)]},
            {"quality_checks": [{"id": str(i), "passed": i != 7} for i in range(8)]},
            {
                "cost_improvement_percent": 14,
                "comparable_token_improvement_percent": 14,
            },
            {"cost_improvement_percent": math.nan},
            {"comparable_token_improvement_percent": math.inf},
            {"cost_improvement_percent": "15"},
            {
                "cost_improvement_percent": None,
                "comparable_token_improvement_percent": None,
            },
            {"wall_time_regression_percent": 16},
            {"wall_time_regression_percent": True},
        ]
        for change in cases:
            with self.subTest(change=change):
                self.assertEqual(
                    routed(policy=policy, evidence=attestation(policy, **change))[
                        "action"
                    ],
                    "parent",
                )

    def test_one_explicitly_unavailable_gain_is_allowed(self):
        policy = enabled_policy()
        evidence = attestation(
            policy,
            cost_improvement_percent=None,
            comparable_token_improvement_percent=20,
        )
        self.assertEqual(routed(policy=policy, evidence=evidence)["action"], "dispatch")

    def test_attestation_rejects_missing_or_privacy_leaking_fields(self):
        policy = enabled_policy()
        for field in tuple(route_execution.ATTESTATION_FIELDS):
            value = attestation(policy)
            del value[field]
            self.assertEqual(routed(policy=policy, evidence=value)["action"], "parent")
        for field in ("hostname", "absolute_path", "prompt"):
            value = attestation(policy)
            value[field] = "secret"
            self.assertEqual(routed(policy=policy, evidence=value)["action"], "parent")

    def test_cli_task_class_and_positional_compatibility(self):
        cases = [
            (["mechanical"], "gpt-5.6-luna"),
            (["--task-class", "mechanical"], "gpt-5.6-luna"),
            (["select", "--task-class", "exploration"], "gpt-5.6-terra"),
        ]
        for args, model in cases:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "route_execution.py"),
                    *args,
                    "--experimental",
                    "--available-model",
                    model,
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["action"], "evaluation_only")


if __name__ == "__main__":
    unittest.main()
