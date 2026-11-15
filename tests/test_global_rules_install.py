from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import install_global_rules as installer  # noqa: E402


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def apply(home):
    reviewed = installer.plan(home)
    return installer.deploy(home, True, expected_plan_sha256=installer.plan_digest(reviewed), allow_test_root=True)


class GlobalRulesInstallTests(unittest.TestCase):
    def test_cli_uses_active_home_with_spaces_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "Codex Home With Spaces"
            environment = {**os.environ, "CODEX_HOME": str(home)}
            command = [sys.executable, str(ROOT / "scripts/install_global_rules.py")]
            preview = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertEqual(json.loads(preview.stdout)["action"], "create")
            self.assertFalse(home.exists())
            preserved = {
                "config.toml": b"model = 'personal'\r\n",
                "hooks.json": b'{"hooks":{"Stop":[]}}\n',
                "schedules/user.json": b'{"enabled":true}\n',
            }
            for relative, content in preserved.items():
                path = home / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            pin = json.loads(subprocess.check_output(command, env=environment, text=True))["plan_sha256"]
            first = subprocess.run([*command, "--apply", "--expected-plan-sha256", pin], env=environment, capture_output=True, text=True, check=False)
            self.assertEqual(first.returncode, 0, first.stderr)
            unrelated = home / "keep.txt"
            unrelated.write_text("keep", encoding="utf-8")
            before = (home / "AGENTS.md").read_bytes()
            pin = json.loads(subprocess.check_output(command, env=environment, text=True))["plan_sha256"]
            second = subprocess.run([*command, "--apply", "--expected-plan-sha256", pin], env=environment, capture_output=True, text=True, check=False)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(second.stdout)["action"], "same")
            self.assertEqual((home / "AGENTS.md").read_bytes(), before)
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")
            for relative, content in preserved.items():
                self.assertEqual((home / relative).read_bytes(), content)

    def test_identical_existing_rules_are_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "codex"
            home.mkdir()
            (home / "AGENTS.md").write_bytes(installer.SOURCE.read_bytes())
            result = apply(home)
            self.assertEqual(result["action"], "adopt")
            manifest = json.loads((home / installer.MANIFEST_RELATIVE).read_text(encoding="utf-8"))
            self.assertEqual(manifest["files"]["AGENTS.md"], installer.sha256(installer.SOURCE))

    def test_unknown_or_locally_modified_rules_are_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "codex"
            home.mkdir()
            target = home / "AGENTS.md"
            target.write_text("personal rules", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "conflict"):
                apply(home)
            self.assertEqual(target.read_text(encoding="utf-8"), "personal rules")
            self.assertEqual([path.name for path in home.iterdir()], ["AGENTS.md"])

    def test_managed_update_creates_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "codex"
            target = home / "AGENTS.md"
            manifest = home / installer.MANIFEST_RELATIVE
            target.parent.mkdir(parents=True)
            old = b"previous managed rules\n"
            target.write_bytes(old)
            installer.atomic_write_json(manifest, {
                "schema_version": 1, "owner": installer.OWNER,
                "files": {"AGENTS.md": digest(old)},
            })
            result = apply(home)
            self.assertEqual(result["action"], "update")
            backup = Path(result["backup"])
            self.assertEqual((backup / "AGENTS.md").read_bytes(), old)
            self.assertTrue((backup / manifest.name).exists())
            self.assertEqual(target.read_bytes(), installer.SOURCE.read_bytes())
            self.assertTrue(backup.is_relative_to(home / "maintenance"))
            self.assertFalse((home / "portable-harness/global-rules-backups").exists())

    def test_preview_hash_is_required_and_binds_source_and_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "codex"
            source = root / "source.md"
            source.write_text("reviewed", encoding="utf-8")
            with mock.patch.object(installer, "SOURCE", source), mock.patch.object(installer, "REPO_ROOT", root):
                preview = installer.deploy(home, False)
                with self.assertRaisesRegex(ValueError, "plan hash"):
                    installer.deploy(home, True, allow_test_root=True)
                with self.assertRaisesRegex(ValueError, "plan hash"):
                    installer.deploy(root / "other", True, expected_plan_sha256=preview["plan_sha256"], allow_test_root=True)
                source.write_text("changed after preview", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "plan hash"):
                    installer.deploy(home, True, expected_plan_sha256=preview["plan_sha256"], allow_test_root=True)
                self.assertFalse(home.exists())

    def test_publication_uses_verified_bytes_even_if_source_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "codex"
            source = root / "source.md"
            source.write_bytes(b"reviewed bytes")
            publish = installer._atomic_publish

            def mutate_then_publish(raw, target):
                source.write_bytes(b"changed during publication")
                publish(raw, target)

            with mock.patch.object(installer, "SOURCE", source), mock.patch.object(installer, "REPO_ROOT", root), mock.patch.object(installer, "_atomic_publish", side_effect=mutate_then_publish):
                apply(home)
            self.assertEqual((home / "AGENTS.md").read_bytes(), b"reviewed bytes")

    def test_stale_source_and_target_pins_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "codex"
            source = root / "source.md"
            source.write_text("one", encoding="utf-8")
            with mock.patch.object(installer, "SOURCE", source), mock.patch.object(installer, "REPO_ROOT", root):
                reviewed = installer.plan(home)
                source.write_text("two", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "source changed"):
                    installer.apply_plan(home, reviewed)
                source.write_text("one", encoding="utf-8")
                home.mkdir()
                (home / "AGENTS.md").write_text("intervening", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "target changed"):
                    installer.apply_plan(home, reviewed)

    def test_symlink_target_is_rejected_without_touching_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "codex"
            outside = root / "outside.md"
            home.mkdir()
            outside.write_text("outside", encoding="utf-8")
            try:
                os.symlink(outside, home / "AGENTS.md")
            except OSError as exc:
                target = home / "AGENTS.md"
                target.write_text("simulated link target", encoding="utf-8")
                original = Path.is_symlink
                with mock.patch.object(Path, "is_symlink", lambda path: path == target or original(path)):
                    with self.assertRaisesRegex(ValueError, "symlink"):
                        apply(home)
            else:
                # A real out-of-root link can fail containment before the link check.
                with self.assertRaisesRegex(ValueError, "symlink|path escapes allowed root"):
                    apply(home)
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside")
            self.assertFalse((home / installer.MANIFEST_RELATIVE).exists())

    def test_apply_uses_shared_runtime_install_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "codex"
            state = home / "maintenance"
            state.mkdir(parents=True)
            with installer.lease(state / "runtime-install.lock"):
                with self.assertRaises((BlockingIOError, OSError, RuntimeError)):
                    apply(home)
            self.assertFalse((home / "AGENTS.md").exists())
            self.assertFalse((home / installer.MANIFEST_RELATIVE).exists())


if __name__ == "__main__":
    unittest.main()
