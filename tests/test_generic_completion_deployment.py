import contextlib
import copy
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import task_completion as completion
import test_run
import install_maintenance as installer
import doctor
from deployment_state import compare


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifact = self.root / "report.txt"
        self.artifact.write_text("fixture result", encoding="utf-8")
        self.spec = {"schema_version": 1, "task_id": "fixture", "version": "v1",
                     "environment": "isolated", "profile": "artifact",
                     "requirements": [{"id": "report", "type": "artifact", "path": "report.txt",
                                       "sha256": digest(self.artifact)}]}

    def test_artifact_needs_no_ui_or_human(self):
        value = completion.verify(self.spec, self.root)
        self.assertTrue(value["complete"])
        self.assertFalse(value["human_acceptance_verified"])
        self.assertFalse(value["execution_authorized"])

    def test_missing_and_changed_file_never_pass(self):
        self.artifact.unlink()
        self.assertFalse(completion.verify(self.spec, self.root)["complete"])
        self.artifact.write_text("changed")
        self.assertEqual(completion.verify(self.spec, self.root)["checks"][0]["reason"], "evidence_hash_mismatch")

    def test_path_escape_rejected(self):
        for path in ("../report.txt", "/report.txt", "C:/report.txt", "x\\report.txt"):
            with self.subTest(path=path):
                self.spec["requirements"][0]["path"] = path
                self.assertFalse(completion.verify(self.spec, self.root)["complete"])

    def event_spec(self):
        self.spec["profile"] = "ui"
        self.spec["require_human"] = True
        self.spec["operation_id"] = "fixture-operation"
        record = self.root / "event.json"
        record.write_text('{"origin":"real_user","result":"passed"}')
        self.spec["requirements"] = [
            {"id": phase, "type": "event", "path": "event.json", "sha256": digest(record),
             "adapter": "fixture-host", "phase": phase, "origin": "real_user",
             "target": "fixture-" + phase,
             "mechanism": "click" if phase in ("entry", "action") else "observe"}
            for phase in ("entry", "action", "outcome")]
        return self.spec

    def test_file_label_does_not_prove_real_user(self):
        value = completion.verify(self.event_spec(), self.root)
        self.assertFalse(value["complete"])
        self.assertFalse(value["human_acceptance_verified"])
        self.assertEqual(value["checks"][0]["reason"], "source_adapter_required")

    @staticmethod
    def synthetic_adapter(record, req, spec):
        # Synthetic adapter exercising the trusted host interface, not real acceptance.
        return completion.VerifiedEvent(spec["task_id"], spec["version"], spec["environment"],
                                        req["phase"], "passed", req["mechanism"], "synthetic", spec["operation_id"], req["target"])

    def test_synthetic_adapter_cannot_satisfy_real_user(self):
        self.assertFalse(completion.verify(self.event_spec(), self.root, event_verifiers={"fixture-host": self.synthetic_adapter})["complete"])

    def test_original_button_and_optional_restart(self):
        spec = self.event_spec()
        spec["require_human"] = False
        for r in spec["requirements"]:
            r["origin"] = "synthetic"
        adapters = {"fixture-host": self.synthetic_adapter}
        value = completion.verify(spec, self.root, event_verifiers=adapters)
        self.assertTrue(value["complete"])
        self.assertFalse(value["human_acceptance_verified"])
        spec["requirements"][0]["mechanism"] = "GET"
        self.assertFalse(completion.verify(spec, self.root, event_verifiers=adapters)["complete"])

    def test_other_button_or_other_operation_is_not_this_journey(self):
        spec = self.event_spec()
        spec["require_human"] = False
        for r in spec["requirements"]:
            r["origin"] = "synthetic"
        for changes in ({"target": "other-button"}, {"operation_id": "another-journey"}):
            def wrong(record, req, contract):
                return replace(self.synthetic_adapter(record, req, contract), **changes)
            value = completion.verify(spec, self.root, event_verifiers={"fixture-host": wrong})
            self.assertFalse(value["complete"])
            self.assertEqual(value["checks"][0]["reason"], "verified_event_scope_mismatch")

    def test_handoff_needs_receiver(self):
        self.spec["profile"] = "handoff"
        self.assertFalse(completion.verify(self.spec, self.root)["complete"])

    def test_malformed_contracts_and_adapter_errors_are_unknown(self):
        for spec in (None, {}, dict(self.spec, requirements=[]), dict(self.spec, requirements=[{"id": []}])):
            self.assertEqual(completion.verify(spec, self.root)["status"], "unknown")
        spec = self.event_spec()
        def broken(*args):
            raise RuntimeError("broken host")
        value = completion.verify(spec, self.root, event_verifiers={"fixture-host": broken})
        self.assertFalse(value["complete"])
        self.assertEqual(value["checks"][0]["reason"], "source_adapter_error")

    def run_command(self, code):
        with contextlib.redirect_stdout(io.TextIOWrapper(io.BytesIO(), encoding="utf-8")):
            test_run.execute(self.root, [sys.executable, "-c", code], 10, heavy=False)
        paths = sorted((self.root / "maintenance/test-logs").glob("*.json"), key=lambda p: p.stat().st_mtime_ns)
        p = paths[-1]
        row = json.loads(p.read_text())
        return {"id": p.stem, "type": "command", "receipt": p.relative_to(self.root).as_posix(),
                "sha256": digest(p), "command": row["command"], "cwd": row["cwd"]}

    def test_real_commands_failure_not_masked_by_later_success(self):
        first = self.run_command("raise SystemExit(7)")
        second = self.run_command("print('later success')")
        self.spec.update(profile="command", requirements=[first, second])
        value = completion.verify(self.spec, self.root)
        self.assertEqual([r["status"] for r in value["checks"]], ["failed", "passed"])
        self.assertFalse(value["complete"])

    def test_real_command_success_and_tampered_log(self):
        req = self.run_command("print('accepted command')")
        self.spec.update(profile="command", requirements=[req])
        self.assertTrue(completion.verify(self.spec, self.root)["complete"])
        (self.root / req["receipt"]).with_suffix(".log").write_text("rewritten")
        self.assertFalse(completion.verify(self.spec, self.root)["complete"])

    def test_cleanup_failure_and_unfinished_wrapper_never_pass(self):
        req = self.run_command("print('process complete')")
        p = self.root / req["receipt"]
        for update in ({"wrapper_exit_code": 2, "cleanup_error": "locked"},
                       {"wrapper_state": "finalizing"}):
            row = json.loads(p.read_text()); row.update(update)
            p.write_text(json.dumps(row)); req["sha256"] = digest(p)
            self.spec.update(profile="command", requirements=[req])
            self.assertFalse(completion.verify(self.spec, self.root)["complete"])

    def test_final_stdout_failure_leaves_unfinished_receipt(self):
        calls = 0
        def broken_print(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise BrokenPipeError("final output failed")
        with contextlib.redirect_stdout(io.TextIOWrapper(io.BytesIO(), encoding="utf-8")):
            with patch("builtins.print", side_effect=broken_print):
                with self.assertRaises(BrokenPipeError):
                    test_run.execute(self.root, [sys.executable, "-c", "pass"], 10, heavy=False)
        receipts = list((self.root / "maintenance/test-logs").glob("*.json"))
        self.assertEqual(len(receipts), 1)
        row = json.loads(receipts[0].read_text())
        self.assertEqual(row["wrapper_state"], "finalizing")
        self.assertNotIn("wrapper_exit_code", row)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "codex"
        self.home.mkdir()

    def deploy(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return installer.deploy_runtime(self.home, **kwargs)

    def test_runtime_only_preserves_unrelated_and_malformed_config(self):
        saved = {}
        for name in ("config.toml", "hooks.json", "AGENTS.md", "maintenance/local.json"):
            p = self.home / name; p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"existing, possibly malformed content\r\n")
            saved[name] = p.read_bytes()
        self.deploy(apply=True)
        self.deploy(apply=True)
        for name, content in saved.items():
            self.assertEqual((self.home / name).read_bytes(), content)
        self.assertFalse(any(x.status == "FAIL" for x in doctor.check_runtime_deployment(self.home)))

    def test_preview_is_write_free(self):
        self.deploy(apply=False)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_global_rules_drift_is_detected(self):
        self.assertEqual(doctor.check_global_rules(self.home)[0].status, "WARN")
        p = self.home / "AGENTS.md"
        p.write_bytes((installer.REPO_ROOT / "templates/AGENTS.md").read_bytes())
        self.assertEqual(doctor.check_global_rules(self.home)[0].status, "PASS")
        p.write_text("different policy")
        self.assertEqual(doctor.check_global_rules(self.home)[0].status, "FAIL")

    def test_full_installer_keeps_template_and_managed_block(self):
        p = self.home / "AGENTS.md"
        before = (installer.REPO_ROOT / "templates/AGENTS.md").read_bytes()
        p.write_bytes(before)
        with contextlib.redirect_stdout(io.StringIO()):
            installer.deploy(self.home, True, sys.executable)
            installer.deploy(self.home, True, sys.executable)
        self.assertEqual(p.read_bytes(), before)

    def test_both_installers_obey_shared_lease(self):
        state = self.home / "maintenance"
        state.mkdir()
        with installer.lease(state / "runtime-install.lock"):
            for action in (lambda: self.deploy(apply=True),
                           lambda: installer.deploy(self.home, True, sys.executable)):
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises((OSError, RuntimeError)):
                        action()
        self.assertFalse((state / "runtime-ownership.json").exists())

    def test_edit_during_temp_write_is_retained(self):
        self.deploy(apply=True)
        p = self.home / "maintenance/runtime/workload.py"
        p.write_text("# reviewed local version")
        pins = {"workload.py": digest(p)}
        manifest = self.home / "maintenance/runtime-ownership.json"
        before = manifest.read_bytes()
        sync = installer.os.fsync
        def edit(fd):
            p.write_text("# new user edit during copy")
            sync(fd)
        with patch.object(installer.os, "fsync", side_effect=edit):
            with self.assertRaisesRegex(ValueError, "before replacement"):
                self.deploy(apply=True, expected_current=pins)
        self.assertEqual(p.read_text(), "# new user edit during copy")
        self.assertEqual(manifest.read_bytes(), before)

    def test_changed_runtime_is_not_claimed_at_publication(self):
        self.deploy(apply=True)
        manifest = self.home / "maintenance/runtime-ownership.json"
        before = manifest.read_bytes()
        actual_hash = installer.sha256
        target = self.home / "maintenance/runtime/common.py"
        calls = 0
        def inject(path):
            nonlocal calls
            if path == target:
                calls += 1
                if calls == 2:
                    path.write_text("# concurrent user change")
            return actual_hash(path)
        with patch.object(installer, "sha256", side_effect=inject):
            with self.assertRaisesRegex(ValueError, "ownership publication"):
                self.deploy(apply=True)
        self.assertEqual(manifest.read_bytes(), before)
        self.assertEqual(target.read_text(), "# concurrent user change")

    def test_unknown_runtime_edit_requires_exact_pin(self):
        self.deploy(apply=True)
        p = self.home / "maintenance/runtime/workload.py"
        p.write_text("# local edit")
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.deploy(apply=True)
        pins = {"workload.py": digest(p)}
        self.deploy(apply=True, expected_current=pins)
        p.write_text("# a different local edit")
        with self.assertRaisesRegex(ValueError, "reviewed plan"):
            self.deploy(apply=True, expected_current=pins)

    def test_newline_only_and_real_drift(self):
        self.deploy(apply=True)
        p = self.home / "maintenance/runtime/workload.py"
        p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        row = compare(installer.REPO_ROOT / "scripts/workload.py", p)
        self.assertIn(row["status"], ("same", "newline_only"))
        self.assertFalse(any(x.status == "FAIL" for x in doctor.check_runtime_deployment(self.home)))
        p.write_text("# real change")
        self.assertTrue(any(x.status == "FAIL" for x in doctor.check_runtime_deployment(self.home)))


if __name__ == "__main__":
    unittest.main()
