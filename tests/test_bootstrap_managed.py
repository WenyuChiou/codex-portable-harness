from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bootstrap  # noqa: E402
import doctor  # noqa: E402


class ManagedBootstrapTests(unittest.TestCase):
    def test_dry_run_has_no_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / ".codex"
            bootstrap.deploy(home, apply=False)
            self.assertFalse(home.exists())

    def test_apply_writes_manifest_and_five_agents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / ".codex"
            bootstrap.deploy(home, apply=True, allow_test_root=True)
            manifest = json.loads(
                (home / bootstrap.MANIFEST_RELATIVE).read_text(encoding="utf-8")
            )
            self.assertEqual(
                len([name for name in manifest["files"] if name.startswith("agents/")]),
                5,
            )
            self.assertEqual(manifest["owner"], "codex-portable-harness")

    def test_identical_unmanaged_file_is_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / ".codex"
            source, relative = bootstrap.desired_files()[0]
            destination = home / relative
            destination.parent.mkdir(parents=True)
            destination.write_bytes(source.read_bytes())
            actions = bootstrap.deploy(home, apply=True, allow_test_root=True)
            self.assertIn(f"adopt {relative.as_posix()}", actions)

    def test_conflict_preflight_prevents_all_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / ".codex"
            _, relative = bootstrap.desired_files()[0]
            destination = home / relative
            destination.parent.mkdir(parents=True)
            destination.write_text("user drift", encoding="utf-8")
            with self.assertRaises(ValueError):
                bootstrap.deploy(home, apply=True, allow_test_root=True)
            self.assertEqual(
                [path for path in home.rglob("*") if path.is_file()], [destination]
            )

    def test_managed_user_drift_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / ".codex"
            bootstrap.deploy(home, apply=True, allow_test_root=True)
            target = home / "agents" / bootstrap.AGENTS[0]
            target.write_text("user edit", encoding="utf-8")
            with self.assertRaises(ValueError):
                bootstrap.deploy(home, apply=True, allow_test_root=True)
            self.assertEqual(target.read_text(encoding="utf-8"), "user edit")

    def test_repeat_apply_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / ".codex"
            bootstrap.deploy(home, apply=True, allow_test_root=True)
            before = {
                p.relative_to(home).as_posix(): p.read_bytes()
                for p in home.rglob("*")
                if p.is_file()
            }
            actions = bootstrap.deploy(home, apply=True, allow_test_root=True)
            after = {
                p.relative_to(home).as_posix(): p.read_bytes()
                for p in home.rglob("*")
                if p.is_file()
            }
            self.assertEqual(before, after)
            self.assertTrue(all(action.startswith("same ") for action in actions))

    def test_isolated_apply_passes_deployment_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / ".codex"
            bootstrap.deploy(home, apply=True, allow_test_root=True)
            self.assertEqual(doctor.check_skill_deployment(home)[0].status, "PASS")

    def test_symlink_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / ".codex"
            outside = root / "outside"
            outside.mkdir()
            home.mkdir()
            try:
                os.symlink(outside, home / "agents", target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            with self.assertRaises(ValueError):
                bootstrap.deploy(home, apply=True, allow_test_root=True)


if __name__ == "__main__":
    unittest.main()
