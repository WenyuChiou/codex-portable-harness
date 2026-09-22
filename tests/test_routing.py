from __future__ import annotations

import json
import math
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import route_execution  # noqa: E402
import doctor  # noqa: E402
from common import load_json  # noqa: E402

AVAILABLE = ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]
NOW = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
HOST = "1" * 64
SUITE = "2" * 64
RUNTIME = "3" * 64


def enabled_policy():
    value = load_json(ROOT / "policies" / "model-routing.json")
    value["economical_routing_default_enabled"] = True
    value["economical_routing_require_attestation"] = True
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
    def escalated(self, category="exploration", **changes):
        args = {
            "parent_model": "gpt-5.6-sol",
            "escalation_reason": "research_methodology",
            "evidence_refs": ["decision-record:metric-ambiguity"],
            "available_models": ["gpt-5.6-sol", "gpt-6-astra"],
        }
        args.update(changes)
        return route_execution.route_execution(category, **args)

    def test_primary_preference_does_not_change_current_parent(self):
        policy = load_json(ROOT / "policies" / "model-routing.json")
        self.assertEqual(policy["primary_preference"]["model"], "gpt-5.6-sol")
        self.assertFalse(policy["primary_preference"]["changes_host_model_setting"])
        result = route_execution.route_execution("exploration", parent_model="gpt-5.6-sol")
        self.assertEqual((result["action"], result["model"]), ("parent_decides", None))

    def test_explicit_quality_escalation_is_separate_from_economic_proof(self):
        result = self.escalated()
        self.assertEqual((result["action"], result["model"], result["effort"]),
                         ("dispatch", "gpt-6-astra", "high"))
        self.assertEqual(result["role"], "portable-quality-explorer")
        self.assertEqual(result["service_tier"], "unchanged")
        self.assertTrue(result["routing_only_not_execution_authorization"])
        self.assertEqual(result["evidence_refs"], ["decision-record:metric-ambiguity"])

    def test_quality_escalation_preserves_permission_and_plan_gates(self):
        self.assertEqual(self.escalated(permission_denied=True)["action"], "blocked")
        self.assertEqual(self.escalated("implementation", mode="plan",
                                       escalation_reason="complex_debugging")["action"], "plan_only")
        self.assertEqual(self.escalated(mode="plan")["action"], "dispatch")

    def test_escalation_does_not_delegate_governance_or_security_ownership(self):
        for category in ("governance", "security", "trivial"):
            self.assertEqual(self.escalated(category)["action"], "parent")
        result = self.escalated("review", escalation_reason="high_risk_review")
        self.assertEqual((result["action"], result["role"]), ("dispatch", "code-reviewer"))

    def test_quality_escalation_needs_parent_evidence_and_valid_scope(self):
        for changes in ({"parent_model": None}, {"parent_model": " "},
                        {"evidence_refs": []}, {"evidence_refs": [""]},
                        {"evidence_refs": [None]}, {"escalation_reason": "routine_lookup"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.escalated(**changes)["action"], "parent_decides")
        self.assertEqual(self.escalated("implementation")["action"], "parent_decides")

    def test_quality_escalation_can_be_disabled_and_never_falls_back_silently(self):
        policy = load_json(ROOT / "policies" / "model-routing.json")
        policy["quality_escalation"]["enabled"] = False
        self.assertEqual(self.escalated(policy=policy)["action"], "parent_decides")
        self.assertEqual(self.escalated(available_models=["gpt-5.6-sol"])["action"], "parent_decides")

    def test_parent_already_using_astra_does_not_trigger_extra_escalation(self):
        self.assertEqual(self.escalated(parent_model="gpt-6-astra")["action"], "parent")
        result = self.escalated("review", parent_model="gpt-6-astra",
                                escalation_reason="high_risk_review")
        self.assertEqual((result["action"], result["role"], result["model"]),
                         ("dispatch", "code-reviewer", "gpt-6-astra"))

    def test_complex_computer_use_requires_actual_child_capability(self):
        for capability in (False, None, "true"):
            self.assertEqual(self.escalated(escalation_reason="complex_computer_use",
                                           computer_use_available=capability)["action"], "parent_decides")
        self.assertEqual(self.escalated(escalation_reason="complex_computer_use",
                                       computer_use_available=True)["action"], "dispatch")
        self.assertEqual(self.escalated("implementation", mode="plan",
                                       escalation_reason="complex_computer_use",
                                       computer_use_available=True)["action"], "plan_only")

    def test_experimental_quality_selection_stays_evaluation_only(self):
        self.assertEqual(self.escalated(experimental=True)["action"], "evaluation_only")

    def test_quality_escalation_cli(self):
        result = subprocess.run([
            sys.executable, str(ROOT / "scripts" / "route_execution.py"),
            "select", "--task-class", "exploration", "--parent-model", "gpt-5.6-sol",
            "--escalation-reason", "complex_computer_use", "--evidence-ref", "task:visual-layout",
            "--computer-use-available", "--available-model", "gpt-6-astra",
        ], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["action"], "dispatch")

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

    def test_disabled_rollout_routes_to_parent_even_with_valid_proof(self):
        policy = load_json(ROOT / "policies" / "model-routing.json")
        policy["economical_routing_default_enabled"] = False
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

    def test_daily_routes_dispatch_without_benchmark_admission(self):
        for category, model in zip(("mechanical", "exploration", "implementation"), AVAILABLE):
            with self.subTest(category=category):
                result = route_execution.route_execution(category, available_models=AVAILABLE)
                self.assertEqual((result["action"], result["model"]), ("dispatch", model))
                self.assertFalse(result["attestation_valid"])
                self.assertTrue(result["routing_only_not_execution_authorization"])
                self.assertEqual(result["service_tier"], "unchanged")

    def test_daily_routing_preserves_scope_and_availability_guards(self):
        for category in ("mechanical", "implementation"):
            self.assertEqual(route_execution.route_execution(
                category, mode="plan", available_models=AVAILABLE)["action"], "plan_only")
        self.assertEqual(route_execution.route_execution(
            "exploration", mode="plan", available_models=AVAILABLE)["action"], "dispatch")
        self.assertEqual(route_execution.route_execution(
            "exploration", permission_denied=True, available_models=AVAILABLE)["action"], "blocked")
        self.assertEqual(route_execution.route_execution(
            "exploration", available_models=["gpt-5.6-sol"])["action"], "parent_decides")

    def test_daily_preference_does_not_validate_bad_benchmark_evidence(self):
        result = route_execution.route_execution(
            "mechanical", available_models=AVAILABLE, attestation={"paired_runs": 0})
        self.assertEqual(result["action"], "dispatch")
        self.assertFalse(result["attestation_valid"])

    def test_legacy_or_non_boolean_policy_does_not_remove_proof_gate(self):
        for setting in ("missing", None, "false", 0, True):
            policy = enabled_policy()
            if setting == "missing":
                del policy["economical_routing_require_attestation"]
            else:
                policy["economical_routing_require_attestation"] = setting
            self.assertEqual(route_execution.route_execution(
                "mechanical", policy=policy, available_models=AVAILABLE)["action"], "parent")
        policy = load_json(ROOT / "policies" / "model-routing.json")
        policy["economical_routing_default_enabled"] = "true"
        self.assertEqual(route_execution.route_execution(
            "mechanical", policy=policy, available_models=AVAILABLE)["action"], "parent")

    def test_daily_cli_dispatches_without_experimental_or_attestation_flags(self):
        for category, model in zip(("mechanical", "exploration", "implementation"), AVAILABLE):
            result = subprocess.run([
                sys.executable, str(ROOT / "scripts" / "route_execution.py"),
                "select", "--task-class", category, "--available-model", model,
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual((payload["action"], payload["model"]), ("dispatch", model))
            self.assertFalse(payload["attestation_valid"])

    def test_experimental_is_evaluation_only_without_proof(self):
        result = route_execution.route_execution(
            "mechanical", experimental=True, available_models=AVAILABLE
        )
        self.assertEqual(result["action"], "evaluation_only")
        self.assertIn("evaluation only", result["reason"])

    def test_plan_write_task_is_plan_only(self):
        result = routed("implementation", mode="plan")
        self.assertEqual(result["action"], "plan_only")

    def test_plan_allows_explicit_read_only_mechanical_organization(self):
        for mode in ("plan", "default"):
            result = route_execution.route_execution(
                "mechanical", mode=mode, task_effect="read-only", available_models=AVAILABLE)
            self.assertEqual((result["action"], result["model"], result["role"]),
                             ("dispatch", "gpt-5.6-luna", "portable-mechanical-readonly"))
            self.assertTrue(result["read_only_required"])
            self.assertEqual(result["task_effect"], "read-only")

    def test_plan_blocks_mutating_effects_even_if_class_sounds_read_only(self):
        for category in ("mechanical", "exploration", "review", "trivial"):
            result = route_execution.route_execution(
                category, mode="plan", task_effect="write", available_models=AVAILABLE)
            self.assertEqual(result["action"], "plan_only")
        self.assertEqual(route_execution.route_execution(
            "mechanical", mode="plan", available_models=AVAILABLE)["action"], "plan_only")
        self.assertEqual(route_execution.route_execution(
            "implementation", mode="plan", task_effect="read-only",
            available_models=AVAILABLE)["action"], "plan_only")

    def test_effect_conflicts_and_invalid_values_cannot_dispatch(self):
        for category, effect in (("exploration", "write"), ("review", "write"),
                                 ("implementation", "read-only"), ("mechanical", "readonly"),
                                 ("mechanical", False)):
            self.assertEqual(route_execution.route_execution(
                category, task_effect=effect, available_models=AVAILABLE)["action"], "parent_decides")

    def test_read_only_plan_mechanical_keeps_other_gates(self):
        args = dict(mode="plan", task_effect="read-only", available_models=AVAILABLE)
        self.assertEqual(route_execution.route_execution(
            "mechanical", permission_denied=True, **args)["action"], "blocked")
        self.assertEqual(route_execution.route_execution(
            "mechanical", experimental=True, **args)["action"], "evaluation_only")
        self.assertEqual(route_execution.route_execution(
            "mechanical", policy=enabled_policy(), **args)["action"], "parent")
        args["available_models"] = ["gpt-5.6-sol"]
        self.assertEqual(route_execution.route_execution("mechanical", **args)["action"], "parent_decides")

    def test_plan_read_only_mechanical_cli(self):
        result = subprocess.run([
            sys.executable, str(ROOT / "scripts" / "route_execution.py"), "select",
            "--mode", "plan", "--task-class", "mechanical", "--task-effect", "read-only",
            "--available-model", "gpt-5.6-luna",
        ], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual((payload["action"], payload["model"]), ("dispatch", "gpt-5.6-luna"))
        self.assertEqual(payload["role"], "portable-mechanical-readonly")
        self.assertTrue(payload["read_only_required"])

    def test_read_only_roles_have_native_sandbox(self):
        for name in ("portable-mechanical-readonly", "portable-explorer", "portable-quality-explorer"):
            with self.subTest(name=name):
                text = (ROOT / "agents" / (name + ".toml")).read_text(encoding="utf-8")
                self.assertIn('sandbox_mode = "read-only"', text)

    def test_doctor_rejects_malformed_quality_escalation(self):
        for field, value in (("review_role", "default"),
                             ("model", "gpt-5.6-terra"),
                             ("effort", "medium"),
                             ("exploration_role", "portable-explorer")):
            with self.subTest(field=field):
                policy = load_json(ROOT / "policies" / "model-routing.json")
                policy["quality_escalation"][field] = value
                with patch.object(doctor, "load_json", return_value=policy):
                    self.assertEqual(doctor.check_agent_configuration()[0].status, "FAIL")

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
