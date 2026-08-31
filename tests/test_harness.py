from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bootstrap  # noqa: E402
import doctor  # noqa: E402
import memory_proposal  # noqa: E402
import policy_engine  # noqa: E402
from agent_collab_harness import evaluate_policy, sign_human_record, validate_policy  # noqa: E402
from agent_collab_harness.errors import HarnessValidationError  # noqa: E402
from common import load_json  # noqa: E402


class BootstrapTests(unittest.TestCase):
    def test_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / ".codex"
            actions = bootstrap.deploy(home, apply=False)
            self.assertTrue(any(action.startswith("create ") for action in actions))
            self.assertFalse(home.exists())

    def test_apply_deploys_only_allowlisted_skills(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / ".codex"
            bootstrap.deploy(home, apply=True, allow_test_root=True)
            deployed = sorted(path.relative_to(home).as_posix() for path in home.rglob("*") if path.is_file())
            self.assertTrue(deployed)
            self.assertTrue(all(path.startswith("skills/adaptive-response/") or path.startswith("skills/bounded-agent-harness/") for path in deployed))

    def test_apply_rejects_non_codex_product_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in (".claude", ".gemini", "arbitrary"):
                with self.subTest(name=name):
                    with self.assertRaises(ValueError):
                        bootstrap.deploy(root / name, apply=True)


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_json(ROOT / "policies" / "agent-budget.yaml")

    def checkpoint(self, **metrics: int) -> dict[str, object]:
        defaults = {
            "cycles": 0,
            "tool_calls": 0,
            "elapsed_seconds": 0,
            "transcript_bytes": 0,
            "last_checkpoint_bytes": 0,
            "same_failure_retries": 0,
            "no_evidence_cycles": 0,
            "children_spawned": 0,
            "active_children": 0,
            "task_packet_tokens": 0,
            "parent_summary_tokens": 0,
            "memory_digest_tokens": 0,
            "child_result_words": 0,
        }
        defaults.update(metrics)
        return {
            "schema_version": 1,
            "task_id": "task-1",
            "parent_id": None,
            "status": "running",
            "evidence_refs": [],
            "metrics": defaults,
            "stop_reason": None,
        }

    def test_requests_checkpoint_at_soft_transcript_limit(self) -> None:
        checkpoint = self.checkpoint(transcript_bytes=26 * 1024 * 1024)
        result = policy_engine.evaluate(checkpoint, self.policy)
        self.assertEqual(result["decision"], "checkpoint")
        self.assertFalse(result["spawn_allowed"])

    def test_stops_after_two_no_evidence_cycles(self) -> None:
        checkpoint = self.checkpoint(no_evidence_cycles=2)
        self.assertEqual(policy_engine.evaluate(checkpoint, self.policy)["decision"], "stop")

    def test_legacy_unsigned_override_fails_closed(self) -> None:
        checkpoint = self.checkpoint()
        checkpoint["override"] = {"reason": "approved", "limits": {"max_tool_calls": 100}}
        with self.assertRaises(HarnessValidationError):
            policy_engine.evaluate(checkpoint, self.policy)

    def test_adapter_matches_public_policy_decision(self) -> None:
        legacy = self.checkpoint(tool_calls=7, transcript_bytes=1024)
        public = policy_engine.normalize_checkpoint(legacy, self.policy)
        self.assertEqual(
            policy_engine.evaluate(legacy, self.policy),
            evaluate_policy(public, self.policy).to_dict(),
        )

    def test_legacy_checkpoint_accepts_public_policy_shape(self) -> None:
        public_policy = validate_policy(self.policy)
        result = policy_engine.evaluate(self.checkpoint(tool_calls=7), public_policy)
        self.assertEqual(result["decision"], "continue")
        self.assertEqual(result["policy_id"], "portable-harness-agent-budget-v1")

    def test_valid_signed_public_override_is_delegated(self) -> None:
        key_id = "operator"
        secret = "test-only-signing-secret"
        action_hash = "a" * 64
        public_policy = validate_policy(self.policy)
        public_policy["human_authorization"]["key_hashes"] = {
            key_id: hashlib.sha256(secret.encode("utf-8")).hexdigest()
        }
        checkpoint = policy_engine.normalize_checkpoint(
            self.checkpoint(tool_calls=7), public_policy
        )
        checkpoint["pending_action_hash"] = action_hash
        override = {
            "limit": "max_tool_calls",
            "value": 100,
            "reason": "approved test extension",
            "actor": f"human:{key_id}",
            "timestamp": "2026-08-31T12:00:00Z",
            "affected_action_hash": action_hash,
            "observed_value": 7,
            "authorization": {
                "scheme": "hmac-sha256",
                "key_id": key_id,
                "signature": "0" * 64,
            },
        }
        override["authorization"]["signature"] = sign_human_record(override, secret)
        checkpoint["overrides"] = [override]
        with mock.patch.dict(
            os.environ,
            {"AGENT_COLLAB_HUMAN_KEYS_JSON": json.dumps({key_id: secret})},
        ):
            result = policy_engine.evaluate(checkpoint, public_policy)
        self.assertEqual(result["decision"], "continue")
        self.assertEqual(result["effective_limits"]["max_tool_calls"], 100)

    def test_spawn_is_blocked_at_child_limit(self) -> None:
        checkpoint = self.checkpoint(children_spawned=4)
        result = policy_engine.evaluate(checkpoint, self.policy)
        self.assertFalse(result["spawn_allowed"])
        self.assertEqual(result["decision"], "continue")

    def test_oversized_task_packet_stops(self) -> None:
        checkpoint = self.checkpoint(task_packet_tokens=6001)
        self.assertEqual(policy_engine.evaluate(checkpoint, self.policy)["decision"], "stop")

    def test_every_terminal_state_stops_and_blocks_spawn(self) -> None:
        for state in self.policy["terminal_states"]:
            with self.subTest(state=state):
                checkpoint = self.checkpoint()
                checkpoint["status"] = state
                result = policy_engine.evaluate(checkpoint, self.policy)
                self.assertEqual(result["decision"], "stop")
                self.assertFalse(result["spawn_allowed"])

    def test_every_hard_limit_stops_and_blocks_spawn(self) -> None:
        cases = {
            "cycles": 3,
            "tool_calls": 75,
            "elapsed_seconds": 1800,
            "transcript_bytes": 50 * 1024 * 1024,
            "same_failure_retries": 2,
            "no_evidence_cycles": 2,
        }
        for metric, value in cases.items():
            with self.subTest(metric=metric):
                result = policy_engine.evaluate(self.checkpoint(**{metric: value}), self.policy)
                self.assertEqual(result["decision"], "stop")
                self.assertFalse(result["spawn_allowed"])

    def test_missing_schema_fields_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            policy_engine.evaluate({"metrics": self.checkpoint()["metrics"]}, self.policy)

    def test_policy_cli_uses_nonzero_exit_for_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint_path = Path(temporary) / "checkpoint.json"
            checkpoint_path.write_text(json.dumps(self.checkpoint(tool_calls=75)), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "policy_engine.py"), "evaluate", str(checkpoint_path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 4)
            self.assertEqual(json.loads(result.stdout)["decision"], "stop")

    def test_invalid_cli_checkpoint_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint_path = Path(temporary) / "checkpoint.json"
            checkpoint_path.write_text(json.dumps({"metrics": {}}), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "policy_engine.py"), "evaluate", str(checkpoint_path)],
                capture_output=True,
                text=True,
            )
            decision = json.loads(result.stdout)
            self.assertEqual(result.returncode, 4)
            self.assertEqual(decision["decision"], "stop")
            self.assertFalse(decision["spawn_allowed"])

    def test_policy_cli_uses_checkpoint_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint_path = Path(temporary) / "checkpoint.json"
            checkpoint_path.write_text(
                json.dumps(self.checkpoint(transcript_bytes=26 * 1024 * 1024)),
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "policy_engine.py"), "evaluate", str(checkpoint_path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 3)
            self.assertEqual(json.loads(result.stdout)["decision"], "checkpoint")


class DoctorTests(unittest.TestCase):
    def test_public_harness_contract_is_available(self) -> None:
        finding = doctor.check_public_harness()[0]
        self.assertEqual(finding.status, "PASS")
        self.assertIn("0.4.0", finding.detail)

    def test_short_prefix_inside_normal_word_is_not_secret(self) -> None:
        self.assertFalse(doctor.contains_secret("--task-id task-1"))

    def test_credential_shaped_value_is_secret(self) -> None:
        value = "sk" + "-" + ("A" * 32)
        self.assertTrue(doctor.contains_secret(value))

    def test_host_example_is_semantically_valid(self) -> None:
        doctor.validate_host_manifest(load_json(ROOT / "manifests" / "host.example.json"))

    def test_host_validation_matches_strict_schema_constraints(self) -> None:
        document = load_json(ROOT / "manifests" / "host.example.json")
        invalid_documents = []
        invalid_documents.append({**document, "host_id": ""})
        invalid_documents.append({**document, "unexpected": True})
        invalid_documents.append({**document, "available_tools": ["git", "git"]})
        for invalid in invalid_documents:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    doctor.validate_host_manifest(invalid)

    def test_unexpected_deployed_skill_file_fails_diff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / ".codex"
            bootstrap.deploy(home, apply=True, allow_test_root=True)
            extra = home / "skills" / "adaptive-response" / "unexpected.txt"
            extra.write_text("unexpected", encoding="utf-8")
            finding = doctor.check_skill_deployment(home)[0]
            self.assertEqual(finding.status, "FAIL")
            self.assertIn("unexpected", finding.detail)

    def test_secret_scan_reads_staged_bytes_not_clean_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            candidate = root / "candidate.txt"
            secret = "sk" + "-" + ("A" * 32)
            candidate.write_text(secret, encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "--", "candidate.txt"], check=True)
            candidate.write_text("clean working tree", encoding="utf-8")
            findings = doctor.check_repo_content(root)
            failures = [item for item in findings if item.check == "secret-scan" and item.status == "FAIL"]
            self.assertEqual(len(failures), 1)


class MemoryProposalTests(unittest.TestCase):
    def test_decision_does_not_create_canonical_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = root / "proposals"
            path = memory_proposal.propose(store, root, "task-1", "Verified decision", ["artifact:sha256:123"])
            record = load_json(path)
            self.assertEqual(record["state"], "proposed")
            memory_proposal.decide(store, root, record["id"], "approved", "user")
            self.assertEqual(load_json(path)["state"], "approved")
            self.assertEqual(sorted(item.name for item in root.iterdir()), ["proposals"])

    def test_cannot_decide_twice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary)
            path = memory_proposal.propose(store, store, "task-2", "Outcome", ["test:pass"])
            identifier = load_json(path)["id"]
            memory_proposal.decide(store, store, identifier, "rejected", "user")
            with self.assertRaises(ValueError):
                memory_proposal.decide(store, store, identifier, "approved", "user")

    def test_direct_decision_rejects_invalid_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary)
            path = memory_proposal.propose(store, store, "task-invalid", "Outcome", ["test:pass"])
            identifier = load_json(path)["id"]
            with self.assertRaises(ValueError):
                memory_proposal.decide(store, store, identifier, "not-a-decision", "user")

    def test_decision_rejects_record_id_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary)
            path = memory_proposal.propose(store, store, "task-mismatch", "Outcome", ["test:pass"])
            record = load_json(path)
            record["id"] = "00000000-0000-0000-0000-000000000000"
            path.write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaises(ValueError):
                memory_proposal.decide(store, store, path.stem, "approved", "user")

    def test_decision_rejects_traversal_and_malformed_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary) / ".codex" / "proposals"
            store.mkdir(parents=True)
            for identifier in ("../outside", "../../outside", "not-a-uuid", "C:\\outside", "/tmp/outside"):
                with self.subTest(identifier=identifier):
                    with self.assertRaises(ValueError):
                        memory_proposal.decide(store, store.parent, identifier, "approved", "user")

    def test_decision_rejects_symlinked_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / ".codex"
            store = home / "proposals"
            path = memory_proposal.propose(store, home, "task-link", "Outcome", ["test:pass"])
            outside = root / "outside.json"
            outside.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            path.unlink()
            try:
                os.symlink(outside, path)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            with self.assertRaises(ValueError):
                memory_proposal.decide(store, home, path.stem, "approved", "user")

    def test_retirement_proposal_requires_specific_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary)
            with self.assertRaises(ValueError):
                memory_proposal.propose(store, store, "task-3", "Retire stale item", ["audit:1"], "delete")
            path = memory_proposal.propose(store, store, "task-3", "Retire stale item", ["audit:1"], "delete", "memory:item-7")
            self.assertEqual(load_json(path)["target_ref"], "memory:item-7")

    def test_store_must_remain_inside_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / ".codex"
            outside = root / "outside"
            with self.assertRaises(ValueError):
                memory_proposal.proposal_store(str(outside), home)
            inside = memory_proposal.proposal_store("proposals", home)
            self.assertEqual(inside, (home / "proposals").resolve())

    def test_write_function_rejects_store_outside_allowed_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                memory_proposal.propose(root / "outside", root / ".codex", "task-4", "No escape", ["test:1"])

    def test_empty_task_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary)
            with self.assertRaises(ValueError):
                memory_proposal.propose(store, store, "", "No task", ["test:1"])


if __name__ == "__main__":
    unittest.main()
