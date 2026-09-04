from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import benchmark_autonomy as benchmark  # noqa: E402


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.source, check=True)
        (self.source / "tracked.txt").write_text("same", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.source, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
            cwd=self.source,
            check=True,
        )
        self.host = self.root / "host"
        self.host.mkdir()
        self.verifier = self.host / "verify"
        self.verifier.write_text("authority", encoding="utf-8")
        self.output = self.host / "result.json"

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def runner(**kwargs):
        model = kwargs["command"][kwargs["command"].index("--model") + 1]
        tokens = 80 if model != "parent" else 100
        return {
            "valid": True,
            "passed": True,
            "returned_model": model,
            "usage": {
                "input_tokens": tokens,
                "cached_input_tokens": 10,
                "output_tokens": 10,
            },
        }

    def evaluation(self, **changes):
        arguments = dict(
            source_root=self.source,
            cases_path=benchmark.DEFAULT_CASES,
            verifier_path=self.verifier,
            output=self.output,
            runner=self.runner,
            verifier=lambda **kw: True,
            version_probe=lambda executable: "codex 1.2.3",
            control={"executable": "codex", "model": "parent", "effort": "medium"},
            treatment_models={
                kind: f"route-{kind}"
                for kind in (
                    "mechanical",
                    "read_only_exploration",
                    "multifile_implementation",
                    "interrupted_review_correction",
                )
            },
            treatment_effort="medium",
            expected_version="codex 1.2.3",
            seed=42,
        )
        arguments.update(changes)
        return benchmark.run_offline(**arguments)

    def test_fixture_has_eight_meaningful_balanced_cases(self):
        cases = benchmark.load_cases()
        self.assertEqual(len(cases), 8)
        self.assertTrue(
            all("fixture task" not in case["prompt"].lower() for case in cases)
        )
        for kind in (
            "mechanical",
            "read_only_exploration",
            "multifile_implementation",
            "interrupted_review_correction",
        ):
            self.assertEqual(sum(case["task_type"] == kind for case in cases), 2)

    def test_fixture_paths_and_content_are_validated_before_materialization(self):
        original = json.loads(benchmark.DEFAULT_CASES.read_text(encoding="utf-8"))
        unsafe = (
            "../../outside.txt", "/absolute.txt", "C:\\escape.txt",
            "\\\\server\\share.txt", "dir\\file.txt", "name:stream",
            "trailing. /file",
        )
        for index, name in enumerate(unsafe):
            with self.subTest(name=name):
                document = json.loads(json.dumps(original))
                document["cases"][0]["files"] = {name: "x"}
                path = self.root / f"unsafe-{index}.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "unsafe"):
                    benchmark.load_cases(path)
        document = json.loads(json.dumps(original))
        document["cases"][0]["files"] = {"safe.txt": 123}
        path = self.root / "bad-content.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "strings"):
            benchmark.load_cases(path)

    def test_command_is_safe_argument_list_without_tier_override(self):
        command = benchmark.build_command(
            "codex", model="m", effort="low", sandbox="workspace-write"
        )
        self.assertEqual(command[-2:], ["--json", "-"])
        self.assertIn("--ephemeral", command)
        self.assertNotIn("service_tier", " ".join(command))
        self.assertNotIn("bypass", " ".join(command))

    def test_usage_subtracts_cached_from_total_and_cost_unknown(self):
        self.assertEqual(
            benchmark.normalize_usage(
                {"input_tokens": 100, "cached_input_tokens": 30, "output_tokens": 20}
            ),
            {
                "uncached_input_tokens": 70,
                "cached_input_tokens": 30,
                "output_tokens": 20,
                "cost_microusd": None,
            },
        )
        self.assertIsNone(
            benchmark.normalize_usage(
                {"input_tokens": 10, "cached_input_tokens": 11, "output_tokens": 0}
            )
        )

    def test_version_mismatch_fails_before_runner(self):
        runner = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, "version mismatch"):
            self.evaluation(runner=runner, version_probe=lambda executable: "other")
        runner.assert_not_called()

    def test_seeded_warmups_excluded_and_models_recorded(self):
        runner = mock.Mock(side_effect=self.runner)
        result = self.evaluation(runner=runner)
        self.assertEqual(runner.call_count, 20)
        self.assertEqual(len(result["runs"]), 16)
        self.assertEqual(result["seed"], 42)
        self.assertEqual(result["warmups_per_arm"], 2)
        self.assertTrue(
            all(
                run["model"] == "parent"
                for run in result["runs"]
                if run["arm"] == "control"
            )
        )
        self.assertTrue(
            all(
                run["model"].startswith("route-")
                for run in result["runs"]
                if run["arm"] == "treatment"
            )
        )

    def test_identical_pair_hashes_and_readonly_sandbox(self):
        result = self.evaluation()
        for case in benchmark.load_cases():
            pair = [run for run in result["runs"] if run["case_id"] == case["case_id"]]
            self.assertEqual(pair[0]["fixture_sha256"], pair[1]["fixture_sha256"])
        commands = []

        def runner(**kwargs):
            commands.append(kwargs["command"])
            return self.runner(**kwargs)

        self.evaluation(output=self.host / "second.json", runner=runner)
        self.assertTrue(
            any(
                command[command.index("--sandbox") + 1] == "read-only"
                for command in commands
            )
        )

    def test_fake_clock_captures_narrow_process_verifier_wall(self):
        with mock.patch.object(
            benchmark.time, "monotonic", side_effect=[float(i) for i in range(32)]
        ):
            result = self.evaluation()
        self.assertTrue(all(run["wall_seconds"] == 1 for run in result["runs"]))
        self.assertEqual(result["measurement_scope"], benchmark.MEASUREMENT_SCOPE)
        self.assertFalse(result["functional_replay"]["available"])

    def test_partial_output_and_final_source_status_survive_failure(self):
        calls = 0

        def runner(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 6:
                raise RuntimeError("injected")
            return self.runner(**kwargs)

        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.evaluation(runner=runner)
        saved = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertFalse(saved["complete"])
        self.assertIn("source_status_after", saved)
        self.assertIn("injected", saved["error"])
        snapshots = sorted((self.host / "result.json.progress").glob("*.json"))
        self.assertTrue(snapshots)
        self.assertEqual(
            json.loads(snapshots[-1].read_text(encoding="utf-8"))["latest_snapshot"],
            snapshots[-1].name,
        )
        self.assertFalse(list((self.host / "result.json.progress").glob(".*.tmp")))

    def test_final_publication_permission_error_preserves_completed_nonpromotable_evidence(
        self,
    ):
        with mock.patch.object(
            benchmark,
            "_publish_final",
            side_effect=PermissionError("injected publication denial"),
        ):
            with self.assertRaisesRegex(PermissionError, "publication denial"):
                self.evaluation()
        snapshots = sorted((self.host / "result.json.progress").glob("*.json"))
        saved = json.loads(snapshots[-1].read_text(encoding="utf-8"))
        self.assertTrue(saved["complete"])
        self.assertFalse(saved["promotable"])
        self.assertFalse(saved["gate"]["promotable"])
        self.assertFalse(self.output.exists())

    def test_existing_run_requires_unique_output(self):
        self.evaluation()
        with self.assertRaisesRegex(RuntimeError, "unique output"):
            self.evaluation()

    def test_output_lock_conflict_is_explicit(self):
        with benchmark.output_lock(self.output):
            with self.assertRaisesRegex(RuntimeError, "lock conflict"):
                with benchmark.output_lock(self.output):
                    pass

    def test_verifier_mutation_fails_closed(self):
        changed = False

        def verifier(**kwargs):
            nonlocal changed
            if not changed:
                self.verifier.write_text("mutated", encoding="utf-8")
                changed = True
            return True

        result = self.evaluation(verifier=verifier)
        self.assertFalse(result["verifier_unchanged"])
        self.assertFalse(result["gate"]["promotable"])
        self.assertFalse(result["gate"]["exploratory_pass"])

    def test_source_mutation_detected_in_finally(self):
        changed = False

        def verifier(**kwargs):
            nonlocal changed
            if not changed:
                (self.source / "tracked.txt").write_text("changed", encoding="utf-8")
                changed = True
            return True

        result = self.evaluation(verifier=verifier)
        self.assertFalse(result["source_unchanged"])
        self.assertFalse(result["gate"]["promotable"])

    def test_already_dirty_file_change_is_detected_with_identical_status(self):
        dirty = self.source / "tracked.txt"
        dirty.write_text("dirty-before", encoding="utf-8")
        status_before = benchmark._status(self.source)
        changed = False

        def verifier(**kwargs):
            nonlocal changed
            if not changed:
                dirty.write_text("dirty-after", encoding="utf-8")
                changed = True
            return True

        result = self.evaluation(verifier=verifier)
        self.assertEqual(result["source_status_after"], status_before)
        self.assertNotEqual(
            result["source_fingerprint_after"], result["source_fingerprint_before"]
        )
        self.assertFalse(result["source_unchanged"])
        self.assertFalse(result["gate"]["promotable"])

    def test_gate_is_exploratory_only_and_never_promotable(self):
        result = self.evaluation()
        self.assertTrue(result["gate"]["exploratory_pass"])
        self.assertFalse(result["gate"]["promotable"])
        result["runs"][0]["valid"] = False
        self.assertFalse(benchmark.evaluate_gate(result)["exploratory_pass"])

    def test_model_substitution_is_invalid_not_fallback(self):
        def runner(**kwargs):
            value = self.runner(**kwargs)
            if not kwargs["warmup"] and "route-" in value["returned_model"]:
                value["returned_model"] = "substitute"
            return value

        result = self.evaluation(runner=runner)
        self.assertTrue(
            any(not run["valid"] for run in result["runs"] if run["arm"] == "treatment")
        )
        self.assertFalse(result["gate"]["exploratory_pass"])

    def test_outside_authority_and_output_required(self):
        inside = self.source / "verify"
        inside.write_text("x")
        with self.assertRaisesRegex(ValueError, "outside"):
            self.evaluation(verifier_path=inside)


if __name__ == "__main__":
    unittest.main()
