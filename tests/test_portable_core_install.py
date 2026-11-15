"""Exercise the documented core route from a relocated, isolated source copy."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PortableCoreInstallTests(unittest.TestCase):
    def test_relocated_core_installs_and_runs_without_personal_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "New Codex Home"
            source = home / "portable-harness"
            for directory in ("scripts", "skills", "agents", "policies", "templates", "schemas", "manifests", "exports", "fixtures"):
                if (ROOT / directory).is_dir():
                    shutil.copytree(ROOT / directory, source / directory,
                                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            environment = {**os.environ, "CODEX_HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
            environment.pop("PYTHONPATH", None)
            initialized = subprocess.run(
                ["git", "init", "--quiet", str(source)], capture_output=True,
                text=True, timeout=15, check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            preserved = {
                "config.toml": b'model = "local-choice"\n',
                "hooks.json": b'{"hooks": {}}\n',
                "automations/personal/automation.toml": b'status = "PAUSED"\n',
            }
            for name, raw in preserved.items():
                target = home / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)

            def invoke(relative, *args, expected=0):
                process = subprocess.run(
                    [sys.executable, "-B", "-X", "utf8", str(source / relative), *args],
                    cwd=source, env=environment, capture_output=True, text=True,
                    encoding="utf-8", timeout=60, check=False,
                )
                self.assertEqual(process.returncode, expected, process.stdout + process.stderr)
                return process.stdout

            def files():
                return {str(p.relative_to(home)): p.read_bytes()
                        for p in home.rglob("*") if p.is_file() and ".git" not in p.parts}

            initial = files()
            invoke("scripts/bootstrap.py", "--dry-run")
            invoke("scripts/install_global_rules.py", "--dry-run")
            invoke("scripts/install_maintenance.py", "--runtime-only")
            self.assertEqual(files(), initial, "previews must not write")
            for relative, args in (
                ("scripts/bootstrap.py", ("--apply",)),
                ("scripts/install_global_rules.py", ("--apply",)),
                ("scripts/install_maintenance.py", ("--runtime-only", "--apply")),
            ):
                if relative.endswith("install_global_rules.py"):
                    args = (*args, "--expected-plan-sha256", json.loads(invoke(relative))["plan_sha256"])
                elif relative.endswith("install_maintenance.py"):
                    args = (*args, "--expected-plan-sha256",
                            json.loads(invoke(relative, "--runtime-only"))["plan_sha256"])
                invoke(relative, *args)
            self.assertEqual((home / "AGENTS.md").read_bytes(), (source / "templates/AGENTS.md").read_bytes())
            for manifest in (source / "bootstrap-ownership.json", home / "maintenance/global-rules-ownership.json"):
                owned = json.loads(manifest.read_text(encoding="utf-8"))["files"]
                for name, expected_hash in owned.items():
                    self.assertEqual(hashlib.sha256((home / name).read_bytes()).hexdigest(), expected_hash)
            for installed in (home / "maintenance/runtime").glob("*"):
                if installed.is_file():
                    self.assertEqual(installed.read_bytes(), (source / "scripts" / installed.name).read_bytes())
            invoke("scripts/doctor.py", "--cross-tool", "--diff")

            # The deployed wrapper really executes a tiny child and seals its log.
            runtime = home / "maintenance/runtime/task_run.py"
            completed = subprocess.run(
                [sys.executable, "-B", str(runtime), "--timeout", "10", "--",
                 sys.executable, "-c", "print('portable-core-command')"],
                cwd=source, env=environment, capture_output=True, text=True,
                encoding="utf-8", timeout=30, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("portable-core-command", completed.stdout)
            receipts = list((home / "maintenance/test-logs").glob("*.json"))
            self.assertEqual(len(receipts), 1)
            receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
            self.assertEqual(receipt["wrapper_exit_code"], 0)
            self.assertEqual(receipt["log_sha256"], hashlib.sha256(receipts[0].with_suffix(".log").read_bytes()).hexdigest())
            before = files()
            invoke("scripts/bootstrap.py", "--apply")
            invoke("scripts/install_global_rules.py", "--apply", "--expected-plan-sha256",
                   json.loads(invoke("scripts/install_global_rules.py"))["plan_sha256"])
            invoke("scripts/install_maintenance.py", "--runtime-only", "--apply", "--expected-plan-sha256",
                   json.loads(invoke("scripts/install_maintenance.py", "--runtime-only"))["plan_sha256"])
            after = files()
            # Lease files may be recreated; all deployed/user bytes stay identical.
            for name, raw in before.items():
                if not name.endswith(".lock"):
                    self.assertEqual(after.get(name), raw, name)
            for name, raw in preserved.items():
                self.assertEqual((home / name).read_bytes(), raw)
            self.assertFalse((home / "auth.json").exists())
            self.assertFalse((home / "sessions").exists())
            self.assertEqual(list((home / "automations").rglob("*.toml")), [home / "automations/personal/automation.toml"])


if __name__ == "__main__":
    unittest.main()
