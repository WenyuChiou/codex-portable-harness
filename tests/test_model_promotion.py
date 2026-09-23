from __future__ import annotations

import copy
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import benchmark_role_promotion as benchmark  # noqa: E402
import bootstrap  # noqa: E402
import doctor  # noqa: E402
import model_promotion  # noqa: E402
import route_execution  # noqa: E402
import policy_engine  # noqa: E402
from common import load_json  # noqa: E402
from agent_collab_harness.goals import new_goal, advance_checkpoint  # noqa: E402
import test_harness  # noqa: E402

NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)


def policy():
    return load_json(ROOT / "policies" / "model-routing.json")


def evidence(task_class="mechanical"):
    value = policy()
    baseline = value["routes"][task_class]
    candidate = value["role_promotion"]["candidates"][task_class]
    environment = {"host_id": "a" * 64, "os": "windows", "python_version": "3.13",
                   "codex_version": "0.153.3", "service_tier": "unconfigured"}
    attestation = {"schema_version": 1, "task_class": task_class,
                   "issued_at": "2026-09-22T11:00:00Z", **environment,
                   "policy_sha256": model_promotion.canonical_policy_sha256(value),
                   "suite_sha256": "b" * 64,
                   "baseline_model": baseline["model"], "baseline_effort": baseline["effort"],
                   "candidate_model": candidate["model"], "candidate_effort": candidate["effort"],
                   "evidence_name": f"{task_class}-20260922T120000000000Z.json",
                   "evidence_sha256": "e" * 64,
                   "paired_runs": 8,
                   "quality_checks": [{"id": f"pair-{i}", "passed": True} for i in range(8)],
                   "control_failures": 0, "candidate_failures": 0,
                   "control_timeouts": 0, "candidate_timeouts": 0,
                   "control_retries": 0, "candidate_retries": 0,
                   "sandbox_violations": 0,
                   "median_wall_regression_percent": 10,
                   "median_cost_proxy_regression_percent": -20,
                   "source_unchanged": True}
    return value, environment, attestation


class PromotionTests(unittest.TestCase):
    def test_eight_pairs_quality_and_runtime_are_required(self):
        value, environment, proof = evidence()
        self.assertEqual(model_promotion.valid_attestation(
            proof, value, "mechanical", environment, now=NOW, suite_hash="b" * 64)[0], True)
        mutations = [
            {"paired_runs": 7}, {"quality_checks": proof["quality_checks"][:-1]},
            {"candidate_failures": 1},
            {"control_failures": 1, "candidate_failures": 1},
            {"candidate_retries": 1},
            {"candidate_timeouts": 1}, {"sandbox_violations": 1},
            {"median_wall_regression_percent": 16},
            {"median_cost_proxy_regression_percent": 16},
            {"median_wall_regression_percent": math.nan},
            {"source_unchanged": False}, {"host_id": "c" * 64},
            {"policy_sha256": "c" * 64}, {"suite_sha256": "c" * 64},
            {"candidate_model": "gpt-5.6-luna"},
            {"evidence_sha256": "bad"},
            {"issued_at": "2026-08-01T11:00:00Z"},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                changed = {**proof, **mutation}
                self.assertFalse(model_promotion.valid_attestation(
                    changed, value, "mechanical", environment, now=NOW,
                    suite_hash="b" * 64)[0])

    def test_price_snapshot_and_policy_change_fail_closed(self):
        value, environment, proof = evidence()
        stale = copy.deepcopy(value)
        stale["role_promotion"]["price_checked_at"] = "2026-01-01"
        self.assertFalse(model_promotion.valid_attestation(
            proof, stale, "mechanical", environment, now=NOW, suite_hash="b" * 64)[0])
        self.assertFalse(model_promotion.valid_attestation(
            proof, value, "mechanical", environment, now=NOW,
            suite_hash="d" * 64)[0])

    def test_plan_write_is_provisional_then_execution_reselects(self):
        value, environment, proof = evidence()
        plan = route_execution.route_execution("mechanical", mode="plan", task_effect="write",
                                              available_models=["gpt-5.6-luna"], policy=value)
        self.assertEqual(plan["action"], "plan_only")
        self.assertIsNone(plan["model"])
        self.assertTrue(plan["reselect_on_execution"])
        execute = route_execution.route_execution(
            "mechanical", task_effect="write", policy=value, now=NOW,
            promotion_attestation=proof, promotion_environment=environment,
            promotion_suite_hash="b" * 64, candidate_deployed=True,
            available_models=["gpt-5.6-luna", "gpt-6-luna"])
        self.assertEqual((execute["model"], execute["role"]),
                         ("gpt-6-luna", "portable-mechanical-next"))
        self.assertTrue(execute["promotion_active"])
        missing = route_execution.route_execution(
            "mechanical", policy=value, now=NOW, promotion_attestation=proof,
            promotion_environment=environment, promotion_suite_hash="b" * 64,
            candidate_deployed=True, available_models=["gpt-5.6-luna"])
        self.assertEqual(missing["model"], "gpt-5.6-luna")
        self.assertFalse(missing["promotion_active"])
        denied = route_execution.route_execution(
            "mechanical", permission_denied=True, policy=value,
            promotion_attestation=proof, promotion_environment=environment,
            candidate_deployed=True, available_models=["gpt-6-luna"])
        self.assertEqual(denied["action"], "blocked")

    def test_host_local_json_and_candidate_deployment(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / ".codex"
            path = model_promotion.attestation_path(home, "mechanical")
            path.parent.mkdir(parents=True)
            path.write_text('{"x":1,"x":2}', encoding="utf-8")
            self.assertIsNone(model_promotion.strict_local_json(path, home))
            self.assertFalse(model_promotion.candidate_deployed(
                home, policy()["role_promotion"]["candidates"]["mechanical"]))
            self.assertEqual(model_promotion.service_tier(home), "unconfigured")
            (home / "config.toml").write_text('[profiles.fast]\nservice_tier = "fast"\n', encoding="utf-8")
            self.assertEqual(model_promotion.service_tier(home), "unconfigured")
            (home / "config.toml").write_text('profile = "fast"\n[profiles.fast]\nservice_tier = "fast"\n', encoding="utf-8")
            self.assertEqual(model_promotion.service_tier(home), "fast")

    def test_copied_home_cannot_copy_machine_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / ".codex"
            salt = model_promotion.host_id_path(home)
            salt.parent.mkdir(parents=True)
            salt.write_text("a" * 64, encoding="ascii")
            with patch.object(model_promotion, "_machine_identity", return_value="machine-one"):
                one = model_promotion.host_id(home)
            with patch.object(model_promotion, "_machine_identity", return_value="machine-two"):
                two = model_promotion.host_id(home)
            self.assertNotEqual(one, two)
            self.assertEqual(len(one), 64)

    def test_host_attestation_requires_unchanged_raw_evidence(self):
        value, environment, proof = evidence()
        proof["suite_sha256"] = model_promotion.suite_sha256()
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / ".codex"
            path = model_promotion.attestation_path(home, "mechanical")
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(proof), encoding="utf-8")
            with patch.object(model_promotion, "current_environment", return_value=environment):
                _, admitted, reason = model_promotion.host_attestation(home, value, "mechanical")
            self.assertFalse(admitted)
            self.assertIn("raw evidence", reason)

    def test_bootstrap_admits_then_revokes_only_owned_unchanged_roles(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / ".codex"
            with patch.object(model_promotion, "host_attestation",
                              side_effect=lambda _home, _policy, category:
                              ({}, category == "mechanical", "test")):
                actions = bootstrap.deploy(home, apply=True, allow_test_root=True)
            self.assertTrue(any("portable-mechanical-next.toml" in action for action in actions))
            self.assertFalse(any("portable-explorer-next.toml" in action for action in actions))
            with patch.object(model_promotion, "host_attestation", return_value=(None, False, "expired")):
                self.assertEqual(doctor.check_skill_deployment(home)[0].status, "FAIL")
                bootstrap.deploy(home, apply=True, allow_test_root=True)
            owned = load_json(home / bootstrap.MANIFEST_RELATIVE)["files"]
            self.assertIn("agents/portable-mechanical-next.toml", owned)
            self.assertFalse((home / "agents" / "portable-mechanical-next.toml").exists())
            with patch.object(model_promotion, "host_attestation", return_value=(None, False, "expired")):
                self.assertEqual(doctor.check_skill_deployment(home)[0].status, "PASS")

    def test_doctor_validates_candidate_templates(self):
        self.assertEqual(doctor.check_agent_configuration()[0].status, "PASS")

    def test_resume_and_review_correction_keep_accepted_evidence_and_totals(self):
        budget = load_json(ROOT / "policies" / "agent-budget.yaml")
        old = test_harness.PolicyTests().checkpoint(cycles=3, tool_calls=75,
                                                    elapsed_seconds=1800)
        old["evidence_refs"] = ["accepted:test-output"]
        state = new_goal(policy_engine.normalize_checkpoint(old, budget),
                         goal_id="promotion-lifecycle", next_step="review correction")
        before_totals = dict(state["totals"])
        before_evidence = list(state["task"]["evidence_refs"])
        self.assertTrue(policy_engine.evaluate(state, budget)["auto_continue"])
        resumed = advance_checkpoint(state, budget, request_id="next-slice")
        self.assertEqual(resumed["totals"], before_totals)
        self.assertEqual(resumed["task"]["evidence_refs"], before_evidence)
        plan = route_execution.route_execution("implementation", mode="plan", policy=policy())
        self.assertEqual(plan["action"], "plan_only")
        route = route_execution.route_execution("implementation", policy=policy(),
                                                available_models=["gpt-5.6-sol"])
        self.assertEqual((route["action"], route["model"]), ("dispatch", "gpt-5.6-sol"))


class BenchmarkTests(unittest.TestCase):
    def test_schedule_has_eight_pairs_per_role(self):
        for category in ("mechanical", "exploration", "implementation"):
            entries = benchmark.schedule(benchmark.load_cases(category), 5)
            self.assertEqual(len(entries), 8)
            self.assertEqual(len({entry[0] for entry in entries}), 8)
            self.assertEqual({arm for _, _, order in entries for arm in order},
                             {"control", "candidate"})

    def test_gate_requires_all_quality_and_cost_and_wall(self):
        runs = []
        for index in range(8):
            for arm in ("control", "candidate"):
                runs.append({"pair": str(index), "arm": arm, "fixture_sha256": "x",
                             "ok": True, "verified": True, "timeout": False,
                             "wall_seconds": 1.1 if arm == "candidate" else 1.0,
                             "cost_proxy_usd": 0.9 if arm == "candidate" else 1.0,
                             "retries": 0, "sandbox_violation": 0})
        result = {"runs": runs, "source_unchanged": True}
        self.assertTrue(benchmark.evaluate(result, policy())[0])
        bad = copy.deepcopy(result)
        bad["runs"][1]["verified"] = False
        self.assertFalse(benchmark.evaluate(bad, policy())[0])
        rescued = copy.deepcopy(result)
        rescued["runs"][0]["verified"] = False
        allowed, _, metrics = benchmark.evaluate(rescued, policy())
        self.assertTrue(allowed)
        self.assertEqual((metrics["control_failures"], metrics["candidate_failures"]), (1, 0))
        bad = copy.deepcopy(result)
        for run in bad["runs"]:
            if run["arm"] == "candidate":
                run["wall_seconds"] = 1.16
        self.assertFalse(benchmark.evaluate(bad, policy())[0])

    def test_cost_proxy_requires_comparable_usage(self):
        rate = {"input": 2, "cached_input": 0.2, "output": 10}
        self.assertEqual(benchmark.cost_proxy(None, rate), None)
        self.assertAlmostEqual(benchmark.cost_proxy({"uncached_input_tokens": 1000000,
                                                    "cached_input_tokens": 0,
                                                    "output_tokens": 0}, rate), 2)

    def test_static_verifier_never_executes_model_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside.txt"
            workspace = root / "fixture"
            workspace.mkdir()
            (workspace / "math_ops.py").write_text(
                "from pathlib import Path\nPath(" + repr(str(outside)) + ").write_text('unsafe')\n"
                "def total(items):\n    return sum(items)\n", encoding="utf-8")
            (workspace / "report.py").write_text(
                "from math_ops import total\ndef total_prices(items):\n    return total(items)\n", encoding="utf-8")
            self.assertFalse(benchmark.verify_ast_rule("sum-and-report", workspace))
            self.assertFalse(outside.exists())

    def test_static_verifier_requires_imports_and_rejects_top_level_effects(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "math_ops.py").write_text(
                "def total(items):\n    return sum(items)\n", encoding="utf-8")
            (workspace / "report.py").write_text(
                "def total_prices(items):\n    return total(items)\n", encoding="utf-8")
            self.assertFalse(benchmark.verify_ast_rule("sum-and-report", workspace))
            (workspace / "report.py").write_text(
                "from math_ops import total\ndef total_prices(items):\n    return total(items)\n",
                encoding="utf-8")
            self.assertTrue(benchmark.verify_ast_rule("sum-and-report", workspace))
            (workspace / "validator.py").write_text(
                "def valid_slug(value):\n"
                "    return isinstance(value, str) and bool(re.fullmatch(r'[a-z0-9-]+', value))\n",
                encoding="utf-8")
            (workspace / "service.py").write_text(
                "from validator import valid_slug\ndef is_accepted(value):\n    return valid_slug(value)\n",
                encoding="utf-8")
            self.assertFalse(benchmark.verify_ast_rule("slug-and-service", workspace))

    def test_static_rules_check_slug_id_and_chained_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "validator.py").write_text(
                "import re\ndef valid_slug(value):\n"
                "    return isinstance(value, str) and bool(re.fullmatch(r'[a-z0-9-]+', value))\n",
                encoding="utf-8")
            (workspace / "service.py").write_text(
                "from validator import valid_slug\ndef is_accepted(value):\n    return valid_slug(value)\n",
                encoding="utf-8")
            self.assertTrue(benchmark.verify_ast_rule("slug-and-service", workspace))
            (workspace / "validator.py").write_text(
                "def valid_id(value):\n    return isinstance(value, str) and bool(value)\n",
                encoding="utf-8")
            self.assertTrue(benchmark.verify_ast_rule("nonempty-id", workspace))
            accepted = ("class StorageError(Exception):\n    pass\n"
                        "def save(writer, value):\n    try:\n        writer(value)\n"
                        "    except Exception as exc:\n        raise StorageError('save failed') from exc\n")
            (workspace / "storage.py").write_text(accepted, encoding="utf-8")
            self.assertTrue(benchmark.verify_ast_rule("save-exception-chain", workspace))
            rejected = (
                accepted.replace("StorageError(Exception)", "StorageError(Exception, MissingBase)"),
                accepted.replace("writer(value)", "writer(value, unexpected=1)"),
                accepted.replace("StorageError('save failed')", "StorageError(undefined_name)"),
            )
            for candidate in rejected:
                (workspace / "storage.py").write_text(candidate, encoding="utf-8")
                self.assertFalse(benchmark.verify_ast_rule("save-exception-chain", workspace))

    def test_output_link_is_rejected_before_hash_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "fixture"
            workspace.mkdir()
            outside = root / "outside.txt"
            outside.write_text("do not read", encoding="utf-8")
            try:
                os.symlink(outside, workspace / "link.txt")
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            with self.assertRaises(ValueError):
                benchmark.safe_fixture_tree(workspace)

    def test_empty_directory_changes_read_only_fixture_integrity(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "source.txt").write_text("a", encoding="utf-8")
            before, names = benchmark.safe_fixture_tree(workspace)
            self.assertEqual(names, {"source.txt"})
            (workspace / "empty").mkdir()
            after, names = benchmark.safe_fixture_tree(workspace)
            self.assertNotEqual(before, after)
            self.assertIn("empty/", names)

    def test_unreadable_directory_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)

            def unreadable(_root, **options):
                options["onerror"](PermissionError("denied"))
                yield from ()

            with patch.object(benchmark.os, "walk", side_effect=unreadable):
                with self.assertRaises(ValueError):
                    benchmark.safe_fixture_tree(workspace)

    def test_codex_arms_disable_bytecode_equally(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(benchmark.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], 0, "", "")) as invoked:
                benchmark.run_codex(command=["codex", "exec"], prompt="test",
                                    cwd=Path(temporary), timeout=1)
            self.assertEqual(invoked.call_args.kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1")

    def test_unsupported_candidate_saves_partial_evidence_without_retries(self):
        calls = []

        def rejected(**arguments):
            calls.append(arguments)
            return {"exit_code": 1, "timeout": False, "wall_seconds": 0.1,
                    "stdout": '{"type":"turn.failed"}\n',
                    "stderr": "model is not supported"}

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / ".codex"
            environment = evidence()[1]
            with patch.object(model_promotion, "current_environment", return_value=environment):
                result = benchmark.run_benchmark(home, "mechanical", seed=3, runner=rejected)
            self.assertFalse(result["admitted"])
            self.assertEqual(len(calls), 1)
            self.assertTrue(Path(result["local_evidence_file"]).is_file())
            self.assertTrue(result["source_unchanged"])
            self.assertFalse(model_promotion.attestation_path(home, "mechanical").exists())


if __name__ == "__main__":
    unittest.main()
