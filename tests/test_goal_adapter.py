import json
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import policy_engine  # noqa: E402
import doctor  # noqa: E402
from agent_collab_harness.goals import new_goal, advance_checkpoint  # noqa: E402
from common import load_json  # noqa: E402
import test_harness  # noqa: E402


class GoalAdapterTests(unittest.TestCase):
    def test_context_recovery_uses_public_contract_without_resetting_totals(self):
        p = load_json(ROOT / "policies" / "agent-budget.yaml")
        old = test_harness.PolicyTests().checkpoint(
            cycles=3, tool_calls=75,
            transcript_bytes=p["base_policy"]["limits"]["max_transcript_bytes"],
        )
        old["evidence_refs"] = ["test:passed"]
        state = new_goal(policy_engine.normalize_checkpoint(old, p),
                         goal_id="existing", next_step="verify")
        result = policy_engine.evaluate(state, p)
        self.assertEqual((result["decision"], result["scope"]), ("checkpoint", "action"))
        self.assertFalse(result["auto_continue"])
        self.assertFalse(result["spawn_allowed"])
        self.assertIn("context_compaction_required", result["reasons"])
        before = dict(state["totals"])
        state["task"]["transcript_bytes"] = 0  # Measured fixture packet after compaction.
        self.assertTrue(policy_engine.evaluate(state, p)["auto_continue"])
        advanced = advance_checkpoint(state, p, request_id="after-compaction")
        self.assertEqual(advanced["totals"], before)
        state["requires_human"] = True
        self.assertEqual(policy_engine.evaluate(state, p)["scope"], "goal")

    def test_v1_remains_stopped_and_v2_continues(self):
        p = load_json(ROOT / "policies" / "agent-budget.yaml")
        old = test_harness.PolicyTests().checkpoint(
            cycles=3, tool_calls=75, elapsed_seconds=1800
        )
        old["evidence_refs"] = ["test:passed"]
        self.assertEqual(policy_engine.evaluate(old, p)["decision"], "stop")
        state = new_goal(
            policy_engine.normalize_checkpoint(old, p),
            goal_id="existing",
            next_step="review",
        )
        result = policy_engine.evaluate(state, p)
        self.assertTrue(result["auto_continue"])
        next_state = advance_checkpoint(state, p, request_id="continue")
        self.assertEqual(next_state["totals"]["tool_calls"], 75)

    def test_legacy_migration_cli_preserves_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            # macOS /var is an OS alias: use the real fixture root, without
            # relaxing production rejection of symlinked checkpoint paths.
            root = Path(tmp).resolve()
            old = root / "old.json"
            meta = root / "meta.json"
            new = root / "new.json"
            old.write_text(
                json.dumps(test_harness.PolicyTests().checkpoint()), encoding="utf-8"
            )
            original = old.read_bytes()
            meta.write_text(
                json.dumps({"goal_id": "goal", "next_step": "verify"}), encoding="utf-8"
            )
            argv = [
                "policy_engine.py",
                "migrate",
                str(old),
                "--metadata",
                str(meta),
                "--output",
                str(new),
            ]
            output = io.StringIO()
            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
                result = policy_engine.main(test_home=root)
            self.assertEqual(result, 0, output.getvalue())
            self.assertEqual(old.read_bytes(), original)
            self.assertEqual(json.loads(new.read_text())["schema_version"], 2)

    def test_writes_outside_home_fail_before_store_or_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "allowed"
            outside = root / "outside" / "checkpoint.json"
            for command in ("migrate", "advance"):
                argv = [
                    "policy_engine.py",
                    command,
                    str(outside),
                    "--output",
                    str(outside),
                    "--request-id",
                    "next",
                    "--expected-sha256",
                    "0" * 64,
                ]
                output = io.StringIO()
                with (
                    patch.object(sys, "argv", argv),
                    contextlib.redirect_stdout(output),
                ):
                    result = policy_engine.main(test_home=home)
                self.assertEqual(result, 4)
                self.assertIn("escapes allowed root", output.getvalue())
                self.assertFalse(outside.parent.exists())

    def test_duplicate_keys_fail_closed_for_all_adapter_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkpoint = root / "old.json"
            metadata = root / "meta.json"
            policy = root / "policy.json"
            defaults = {
                checkpoint: json.dumps(test_harness.PolicyTests().checkpoint()),
                metadata: json.dumps({"goal_id": "goal", "next_step": "verify"}),
                policy: (ROOT / "policies" / "agent-budget.yaml").read_text(),
            }
            for invalid in defaults:
                for path, contents in defaults.items():
                    path.write_text(contents, encoding="utf-8")
                invalid.write_text(
                    '{"goal_limits":{"tool_calls":0},"goal_limits":{}}',
                    encoding="utf-8",
                )
                target = root / "new.json"
                argv = [
                    "policy_engine.py",
                    "migrate",
                    str(checkpoint),
                    "--metadata",
                    str(metadata),
                    "--policy",
                    str(policy),
                    "--output",
                    str(target),
                ]
                output = io.StringIO()
                with (
                    patch.object(sys, "argv", argv),
                    contextlib.redirect_stdout(output),
                ):
                    result = policy_engine.main(test_home=root)
                self.assertEqual(result, 4)
                self.assertIn("duplicate JSON key", output.getvalue())
                self.assertFalse(target.exists())

    def test_agent_config_matches_routes(self):
        self.assertEqual(doctor.check_agent_configuration()[0].status, "PASS")

    def test_cli_preflight_is_not_a_model_canary(self):
        with patch("doctor.shutil.which", return_value="codex"):
            for version, expected in (("0.144.1", "WARN"), ("0.153.3", "PASS")):
                probe = subprocess.CompletedProcess([], 0, f"codex-cli {version}")
                with patch("doctor.subprocess.run", return_value=probe):
                    finding = doctor.check_cli_compatibility()[0]
                self.assertEqual(finding.status, expected)
                self.assertIn("canary", finding.detail)
        with patch("doctor.shutil.which", return_value=None):
            self.assertEqual(doctor.check_cli_compatibility()[0].status, "WARN")
