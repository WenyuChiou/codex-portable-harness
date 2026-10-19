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
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import agent_smoke
import audit_deployment
import check_services
import common
import machine_profile
import machine_runtime
import notebooklm_compat
import runtime_probe
import setup_machine
import verify_installation


class HomeResolutionTests(unittest.TestCase):
    def test_configured_home_is_honored(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CODEX_HOME": tmp}):
            self.assertEqual(common.active_codex_home(), Path(tmp).resolve())

    def test_unset_home_defaults(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": ""}):
            self.assertEqual(common.active_codex_home(), (Path.home() / ".codex").resolve())

    def test_relative_and_other_product_homes_are_rejected(self):
        for value in ("relative", str(Path.home()), str(Path(Path.home().anchor)), str(Path.home() / ".claude"), str(Path.home() / ".gemini/subdirectory")):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"CODEX_HOME": value}):
                with self.assertRaises(ValueError):
                    common.active_codex_home()


class ManagedResearchTests(unittest.TestCase):
    def test_windows_long_paths_setting_is_process_local(self):
        process = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "test.existing", "GIT_CONFIG_VALUE_0": "keep"}), mock.patch.object(machine_profile.os, "name", "nt"), mock.patch.object(machine_profile.subprocess, "run", return_value=process) as invocation:
            machine_profile.run(["git", "--version"])
            passed = invocation.call_args.kwargs["env"]
            self.assertEqual(passed["GIT_CONFIG_COUNT"], "2")
            self.assertEqual(passed["GIT_CONFIG_KEY_1"], "core.longpaths")
            self.assertEqual(passed["GIT_CONFIG_VALUE_1"], "true")
            self.assertEqual(os.environ["GIT_CONFIG_COUNT"], "1")

    def test_audit_checks_every_manifest_file_and_reports_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "portable-harness").mkdir()
            (home / "skills/example").mkdir(parents=True)
            (home / "agents").mkdir()
            item = home / "skills/example/SKILL.md"
            item.write_bytes(b"source")
            manifest = {"schema_version": 1, "files": {"skills/example/SKILL.md": common.sha256(item)}}
            common.atomic_write_json(home / "portable-harness/bootstrap-ownership.json", manifest)
            self.assertEqual(audit_deployment.audit(home)["checked_file_count"], 1)
            item.unlink()
            self.assertEqual(audit_deployment.audit(home)["managed_mismatches"], 1)

    def test_plan_does_not_create_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            machine_profile.deploy(home, {"skills/example/SKILL.md": b"example"}, apply=False)
            self.assertFalse(home.exists())

    def test_preflight_collision_leaves_all_files_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "agents").mkdir()
            user = home / "agents/custom.toml"
            user.write_bytes(b"user changes")
            with self.assertRaises(ValueError):
                machine_profile.deploy(home, {"skills/new/SKILL.md": b"new", "agents/custom.toml": b"generated"}, apply=True)
            self.assertEqual(user.read_bytes(), b"user changes")
            self.assertFalse((home / "skills").exists())

    def test_repeat_install_preserves_custom_files_and_detects_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            desired = {"skills/example/SKILL.md": b"pinned source"}
            machine_profile.deploy(home, desired, apply=True)
            custom = home / "agents/custom.toml"
            custom.parent.mkdir()
            custom.write_bytes(b"custom")
            self.assertEqual(machine_profile.deploy(home, desired, apply=True), ["same skills/example/SKILL.md"])
            target = home / "skills/example/SKILL.md"
            target.write_bytes(b"edited")
            with self.assertRaises(ValueError):
                machine_profile.deploy(home, desired, apply=True)
            self.assertEqual(target.read_bytes(), b"edited")
            self.assertEqual(custom.read_bytes(), b"custom")

    def test_exact_legacy_hash_allows_migration_but_not_user_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / "agents/example.toml"
            target.parent.mkdir()
            target.write_bytes(b"old verified adapter")
            digest = common.sha256(target)
            machine_profile.deploy(home, {"agents/example.toml": b"new"}, apply=True, previous={"example": digest})
            target.write_bytes(b"local edit")
            with self.assertRaises(ValueError):
                machine_profile.deploy(home, {"agents/example.toml": b"next"}, apply=True, previous={"example": digest})

    def test_path_traversal_and_links_are_refused(self):
        for name in ("../escape", "/absolute", "drive:escape", "sub\\escape"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                machine_profile.relative_path(name)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            outside = Path(tmp) / "outside"
            home.mkdir(); outside.mkdir()
            try:
                (home / "skills").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(str(exc))
            with self.assertRaises(ValueError):
                machine_profile.deploy(home, {"skills/example/SKILL.md": b"bad"}, apply=True)
            self.assertEqual(list(outside.iterdir()), [])

    def test_missing_or_modified_owned_file_fails_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            machine_profile.deploy(home, {"skills/example/SKILL.md": b"source"}, apply=True)
            manifest = home / "machine-setup/research-ownership.json"
            self.assertEqual(verify_installation.check_ownership(home, manifest)["status"], "PASS")
            (home / "skills/example/SKILL.md").unlink()
            self.assertEqual(verify_installation.check_ownership(home, manifest)["status"], "FAIL")

    def test_source_requires_pinned_commit_and_safe_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            for repo, commit in (("../escape", "a" * 40), ("owner/repo", "main")):
                with self.assertRaises(ValueError):
                    machine_profile.fetch_source(Path(tmp), repo, commit)
            self.assertFalse(list(Path(tmp).iterdir()))

    def test_external_gitdir_cache_is_rejected_before_git_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cache = home / "machine-setup/sources" / ("owner--repo-" + "a" * 40)
            cache.mkdir(parents=True)
            (cache / ".git").write_text("gitdir: outside\n")
            with mock.patch.object(machine_profile, "run") as invocation:
                with self.assertRaises(ValueError):
                    machine_profile.fetch_source(home, "owner/repo", "a" * 40)
                invocation.assert_not_called()

    def test_source_tree_rejects_symlink_blob(self):
        result = mock.Mock(stdout=b"120000 blob abc\tunsafe/link\0")
        with mock.patch.object(machine_profile.subprocess, "run", return_value=result):
            with self.assertRaises(ValueError):
                machine_profile.source_files(Path("cache"), "a" * 40, "unsafe")

    def test_adapted_agent_inherits_model_and_keeps_source_body(self):
        source = b'---\nname: code-reviewer\ndescription: Review code.\nmodel: sonnet\n---\nReport evidence.\n'
        text = machine_profile.adapt_agent("code-reviewer", source, "owner/repo", "a" * 40).decode()
        self.assertNotRegex(text, r"(?m)^model\s*=")
        instructions = json.loads(text.split("developer_instructions = ", 1)[1])
        self.assertIn("Report evidence.", instructions)
        self.assertIn("report-only", instructions)
        self.assertIn("do not edit", instructions)


class CompatibilityTests(unittest.TestCase):
    def test_linked_runtime_is_rejected_before_any_installer_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, outside = Path(tmp) / "codex", Path(tmp) / "outside"
            home.mkdir(); outside.mkdir()
            target = home / "research-runtime"
            if os.name == "nt":
                result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(target), str(outside)], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            else:
                target.symlink_to(outside, target_is_directory=True)
            with mock.patch.object(setup_machine, "run") as invocation:
                with self.assertRaises(ValueError):
                    setup_machine.install(home, {}, "python")
                invocation.assert_not_called()
            self.assertEqual(list(outside.iterdir()), [])
            self.assertFalse((home / "tmp").exists())

    @unittest.skipUnless(os.name == "nt", "Windows entrypoint junction boundary")
    def test_entrypoint_rejects_runtime_junction_before_first_write(self):
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if not executable:
            self.skipTest("PowerShell is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            home, outside = Path(tmp) / "codex", Path(tmp) / "outside"
            home.mkdir(); outside.mkdir()
            target = home / "portable-harness-runtime"
            result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(target), str(outside)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            # Only prerequisite discovery is stubbed. The real write-free Python
            # preflight must reject the junction before venv creation or pip.
            command = "function global:git {}; function global:npm {}; function global:codex {}; & '" + str(ROOT / "setup.ps1").replace("'", "''") + "' -Apply -Python '" + sys.executable.replace("'", "''") + "'"
            result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, env={**os.environ, "CODEX_HOME": str(home)})
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(outside.iterdir()), [])
            self.assertFalse((home / "tmp").exists())

    def test_wrong_research_python_fails_before_creating_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            with mock.patch.object(setup_machine.shutil, "which", return_value="npm"), mock.patch.object(setup_machine, "run", return_value="3.14"):
                with self.assertRaises(ValueError):
                    setup_machine.install(home, {}, "wrong-python")
            self.assertFalse(home.exists())

    @unittest.skipUnless(os.name == "nt", "Windows path bound")
    def test_deep_windows_home_fails_before_environment_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ("long-root-" * 12)
            with mock.patch.object(setup_machine.shutil, "which", return_value="npm"), mock.patch.object(setup_machine, "run", return_value="3.12"):
                with self.assertRaisesRegex(ValueError, "too deep"):
                    setup_machine.install(home, {"research_source_max_path": 111}, "python")
            self.assertFalse(home.exists())

    def test_known_legacy_patch_requires_exact_original_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / "research-runtime/auth.py"
            target.parent.mkdir()
            raw = self.source().encode()
            digest = hashlib.sha256(raw).hexdigest()
            target.write_bytes(b"verified old patch")
            legacy_digest = common.sha256(target)
            backup = home / "old/original.py"
            backup.parent.mkdir()
            backup.write_bytes(b"incorrect original")
            with self.assertRaises(ValueError):
                notebooklm_compat.apply(home, target, digest, {legacy_digest: "old/original.py"})
            backup.write_bytes(raw)
            self.assertEqual(notebooklm_compat.apply(home, target, digest, {legacy_digest: "old/original.py"}), "patched")

    @staticmethod
    def source():
        return ('import inspect\n'
                'def _is_on_notebooklm_homepage(url):\n'
                '    return _NLM_HOST in url and "/login" not in url\n'
                'def f():\n'
                '    launch_kwargs = {}\n'
                '    # The homepage URL must hold for this many consecutive polls before we\n'
                '    if True:\n        if True:\n            if True:\n                if True:\n'
                '                    current_url = page.url\n')

    def test_host_allowlist_rejects_substrings_and_login_pages(self):
        namespace = {}
        exec(notebooklm_compat.patched_source(self.source()), namespace)
        check = namespace["_is_on_notebooklm_homepage"]
        self.assertTrue(check("https://notebook.google.com/"))
        self.assertTrue(check("https://notebooklm.google.com/"))
        for url in ("https://notebook.google.com.evil.invalid/", "https://evil.invalid/notebooklm.google.com", "http://notebook.google.com/", "https://notebook.google.com/login"):
            self.assertFalse(check(url))

    def test_unknown_patch_target_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / "research-runtime/auth.py"
            target.parent.mkdir()
            target.write_bytes(b"user patch")
            with self.assertRaises(ValueError):
                notebooklm_compat.apply(home, target, "0" * 64)
            self.assertEqual(target.read_bytes(), b"user patch")
            self.assertFalse((home / "machine-setup").exists())

    def test_patch_is_idempotent_and_keeps_exact_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / "research-runtime/auth.py"
            target.parent.mkdir()
            original = self.source().encode()
            target.write_bytes(original)
            expected = hashlib.sha256(original).hexdigest()
            self.assertEqual(notebooklm_compat.apply(home, target, expected), "patched")
            self.assertEqual(notebooklm_compat.apply(home, target, expected), "same")
            self.assertEqual((home / "machine-setup/compatibility/notebooklm-auth.upstream.py").read_bytes(), original)

    def test_runtime_probe_refuses_system_python(self):
        with mock.patch.object(runtime_probe.sys, "prefix", "same"), mock.patch.object(runtime_probe.sys, "base_prefix", "same"):
            with self.assertRaises(RuntimeError):
                runtime_probe.probe(Path("same"), False)

    def test_runtime_guidance_preserves_user_text_and_refuses_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "AGENTS.md").write_text("Personal guidance\n")
            machine_runtime.configure(home, [home / "tools"])
            before = (home / "AGENTS.md").read_bytes()
            machine_runtime.configure(home, [home / "tools"])
            self.assertEqual(before, (home / "AGENTS.md").read_bytes())
            self.assertTrue(before.startswith(b"Personal guidance"))
            (home / "AGENTS.md").write_text(before.decode().replace("Do not substitute", "Local edit"))
            with self.assertRaises(ValueError):
                machine_runtime.configure(home, [home / "tools"])

    def test_modified_activation_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            machine_runtime.configure(home, [home / "tools"])
            activation = home / "machine-setup/activate.ps1"
            activation.write_text("personal edits\n")
            with self.assertRaises(ValueError):
                machine_runtime.configure(home, [home / "tools"])
            self.assertEqual(activation.read_text(), "personal edits\n")

    @unittest.skipUnless(os.name == "nt", "Windows binary installer")
    def test_antigravity_checksum_failure_never_publishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            release = {"url": "https://example.invalid/agy", "sha512": "0" * 128, "version": "test"}
            with mock.patch.object(setup_machine.shutil, "which", return_value=None), mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}), mock.patch.object(setup_machine.os, "name", "nt"), mock.patch.object(setup_machine, "https_bytes", side_effect=[json.dumps(release).encode(), b"bad"]):
                # pathlib's concrete path class was selected before patching os.name.
                with mock.patch.object(setup_machine.platform, "machine", return_value="AMD64"):
                    with self.assertRaises(ValueError):
                        setup_machine.antigravity(home, {"antigravity_manifest_base": "https://example.invalid"})
            self.assertFalse((home / "tools/agy/agy.exe").exists())


class NativeAcceptanceTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows ACL entrypoint")
    def test_entrypoint_repairs_both_original_virtual_environments(self):
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if not executable:
            self.skipTest("PowerShell is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            home, repo = Path(tmp) / "codex", Path(tmp) / "repo"
            (home / "portable-harness-runtime/Scripts").mkdir(parents=True)
            (home / "machine-setup").mkdir()
            (repo / "scripts").mkdir(parents=True)
            launcher = home / "portable-harness-runtime/Scripts/python.exe"
            launcher.touch()
            (home / "machine-setup/activate.ps1").write_text("")
            # No installers, model calls or ACL changes run in this fixture.
            # Stub only checked command execution, leaving repair dispatch and
            # argument binding intact. Absolute-path function interception works
            # in Windows PowerShell but PowerShell 7 resolves the native file.
            setup = (ROOT / "setup.ps1").read_text(encoding="utf-8")
            self.assertEqual(setup.count("& $Executable @Arguments"), 1)
            (repo / "setup.ps1").write_text(setup.replace("& $Executable @Arguments", "$global:LASTEXITCODE = 0"), encoding="utf-8")
            # The repair stub records the exact launchers handed off by setup.
            (repo / "scripts/repair_windows_python.ps1").write_text(
                "param([switch]$Apply, [string]$PythonPath)\n"
                "if (-not $Apply -or -not $PythonPath) { throw 'Missing repair target' }\n"
                "Add-Content -LiteralPath (Join-Path $env:CODEX_HOME 'repairs.txt') -Value $PythonPath\nexit 0\n")
            literal = lambda path: "'" + str(path).replace("'", "''") + "'"
            command = "function global:git {}; function global:npm {}; function global:codex {}; "
            command += "& " + literal(repo / "setup.ps1") + " -Apply -RepairPythonAcl -Python " + literal(launcher)
            result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, env={**os.environ, "CODEX_HOME": str(home)})
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            repaired = (home / "repairs.txt").read_text(encoding="utf-8-sig").splitlines()
            # Windows runners may expose TEMP through its DOS 8.3 alias;
            # PowerShell expands that alias when resolving CODEX_HOME.
            self.assertEqual([Path(path).resolve() for path in repaired],
                             [launcher.resolve(), (home / "research-runtime/Scripts/python.exe").resolve()])

    def test_failed_completion_checkpoint_preserves_terminal_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            folder = home / "run"
            folder.mkdir()
            common.atomic_write_json(folder / "plan.json", {"cases": [{"agent_type": "example"}], "prior_evidence": ["earlier-failure"]})
            (folder / "events.jsonl").write_text(json.dumps({"type": "thread.started", "thread_id": "test-parent"}) + "\n")
            trace = home / "trace.jsonl"
            trace.write_text(json.dumps({"timestamp": "2026-10-18T00:00:00Z", "payload": {}}) + "\n")
            with mock.patch.object(agent_smoke, "trace_for", return_value=trace):
                report = agent_smoke.gate(home, folder, 1, complete=True, passed=False)
            self.assertEqual(report["checkpoint"]["status"], "error")
            self.assertIn("earlier-failure", report["checkpoint"]["evidence_refs"])
            self.assertFalse(report["decision"]["spawn_allowed"])

    def test_online_checks_require_explicit_selection(self):
        with mock.patch.object(sys, "argv", ["check_services.py"]), mock.patch.object(check_services, "notebooklm") as server, mock.patch.object(check_services, "antigravity") as agy, mock.patch("builtins.print"):
            self.assertEqual(check_services.main(), 2)
            server.assert_not_called(); agy.assert_not_called()

    def test_antigravity_failed_canary_does_not_expose_provider_output(self):
        fake = mock.Mock(returncode=0, stdout=json.dumps({"status": "FAIL", "response": "private-provider-output"}))
        with mock.patch.object(check_services.subprocess, "run", return_value=fake):
            report = check_services.antigravity("agy")
        self.assertFalse(report["authenticated_canary"])
        self.assertNotIn("private-provider-output", json.dumps(report))

    def test_changed_compatibility_file_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            site = home / "research-runtime/Lib/site-packages"
            target = site / "research_hub/notebooklm/auth.py"
            target.parent.mkdir(parents=True)
            original = CompatibilityTests.source().encode()
            target.write_bytes(original)
            notebooklm_compat.apply(home, target, hashlib.sha256(original).hexdigest())
            self.assertEqual(verify_installation.check_compatibility(home, site)["status"], "PASS")
            target.write_bytes(b"unexpected upgrade")
            self.assertEqual(verify_installation.check_compatibility(home, site)["status"], "FAIL")

    def test_wrong_identity_or_fabricated_final_is_rejected(self):
        cases = [{"agent_type": "portable-worker", "required_terms": ["OK"]}]
        result = {"cases": [{"agent_type": "portable-worker", "agent_id": "/root/test", "status": "completed", "answer": "OK"}]}
        evidence = {"dispatch": [{"agent_type": "portable-worker", "agent_id": "/root/test", "model": None, "fork_turns": "none"}], "finals": {"/root/test": "OK"}}
        self.assertTrue(agent_smoke.accepted(cases, result, evidence))
        evidence["finals"]["/root/test"] = "Different actual answer"
        self.assertFalse(agent_smoke.accepted(cases, result, evidence))
        evidence["finals"]["/root/test"] = "OK"
        evidence["dispatch"][0]["agent_type"] = "default"
        self.assertFalse(agent_smoke.accepted(cases, result, evidence))

    def test_model_override_and_failed_case_are_not_success(self):
        cases = [{"agent_type": "portable-worker", "required_terms": ["OK"]}]
        result = {"cases": [{"agent_type": "portable-worker", "agent_id": "/root/test", "status": "blocked", "answer": "OK"}]}
        evidence = {"dispatch": [{"agent_type": "portable-worker", "agent_id": "/root/test", "model": None, "fork_turns": "none"}], "finals": {"/root/test": "OK"}}
        self.assertFalse(agent_smoke.accepted(cases, result, evidence))
        result["cases"][0]["status"] = "completed"
        evidence["dispatch"][0]["model"] = "other-model"
        self.assertFalse(agent_smoke.accepted(cases, result, evidence))

    def test_pending_auth_never_turns_into_authenticated_pass(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(verify_installation, "check_ownership", return_value={"status": "PASS"}), mock.patch.object(verify_installation, "command_check", return_value={"status": "PASS"}):
            report = verify_installation.verify(Path(tmp), sandbox=False)
            self.assertEqual(report["authenticated_services"], "NOT_CHECKED")
            self.assertTrue(report["pending"])
            self.assertFalse(report["local_checks_passed"])

    def test_powershell_parses(self):
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if not executable:
            self.skipTest("PowerShell is unavailable")
        for path in (ROOT / "setup.ps1", ROOT / "scripts/repair_windows_python.ps1"):
            literal = str(path).replace("'", "''")
            command = "$e=$null; $t=$null; [System.Management.Automation.Language.Parser]::ParseFile('" + literal + "',[ref]$t,[ref]$e) | Out-Null; if ($e.Count) { $e; exit 1 }"
            result = subprocess.run([executable, "-NoProfile", "-Command", command], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_powershell_rejects_unexpanded_home_before_any_setup(self):
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if not executable:
            self.skipTest("PowerShell is unavailable")
        environment = {**os.environ, "CODEX_HOME": "~/.codex"}
        result = subprocess.run([executable, "-NoProfile", "-File", str(ROOT / "setup.ps1")], capture_output=True, env=environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"absolute path", result.stderr)


if __name__ == "__main__":
    unittest.main()
