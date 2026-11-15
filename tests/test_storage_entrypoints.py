import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
import maintenance as m
import install_maintenance as installer
import workload
import task_run as tasks
import storage_lifecycle as lifecycle


class StorageEntrypoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()/"codex"
        self.home.mkdir()
        (self.home/"maintenance").mkdir()
        (self.home/"maintenance/local.json").write_text(json.dumps({"storage":{"pinned":[str(self.home/"maintenance/reports")]}}))
        self.now = time.time()+200*86400

    def invoke(self, *args):
        with patch.object(m, "active_codex_home", return_value=self.home), patch.object(m.time, "time", return_value=self.now), patch.object(sys, "argv", ["maintenance.py", *args]), contextlib.redirect_stdout(io.StringIO()):
            return m.main()

    def fixture(self):
        path=self.home/"visualizations/2025/01/01/old-output"
        path.mkdir(parents=True)
        (path/"payload").write_text("owned output")
        return path

    def plan(self):
        target=self.home/"maintenance/preview.json"
        self.assertEqual(self.invoke("--only", "storage", "--plan", str(target)), 0)
        return target

    def test_preview_is_nondestructive_and_records_plan(self):
        path=self.fixture()
        plan=self.plan()
        self.assertTrue(path.exists())
        data=json.loads(plan.read_text())
        self.assertTrue(any(x["path"]==str(path) for x in data["mutations"]))
        journals=list((self.home/"maintenance/reports").glob("*.jsonl"))
        events=[json.loads(line) for line in journals[0].read_text().splitlines()]
        self.assertTrue(any(x["action"]=="delete-expired" and not x["apply"] for x in events))

    def test_storage_journal_reuses_the_checked_candidate_snapshot(self):
        self.fixture()
        with patch.object(m,'snapshot',side_effect=AssertionError('duplicate journal snapshot')):
            self.plan()

    def test_preview_syncs_complete_journal_before_plan_without_per_item_flushes(self):
        for number in range(30):
            path=self.home/'visualizations'/str(number)/'outcome'
            path.parent.mkdir(parents=True);path.write_text('fixture')
        original=os.fsync
        with patch.object(os,'fsync',wraps=original) as sync:
            plan=self.plan()
        self.assertLessEqual(sync.call_count,2)
        self.assertGreaterEqual(sync.call_count,1)
        self.assertEqual(len(json.loads(plan.read_text())['mutations']),30)

    def test_apply_requires_reviewed_plan_and_matching_hash(self):
        self.fixture()
        with self.assertRaises(SystemExit):
            self.invoke("--apply")
        plan=self.plan()
        with self.assertRaises(SystemExit):
            self.invoke("--only", "storage", "--apply", "--approved-plan", str(plan), "--plan-sha256", "0"*64)

    def test_matching_apply_removes_only_the_reviewed_candidate(self):
        path=self.fixture()
        plan=self.plan()
        self.assertEqual(self.invoke("--only", "storage", "--apply", "--approved-plan", str(plan), "--plan-sha256", m.sha256(plan)), 0)
        self.assertFalse(path.exists())
        self.assertTrue(plan.exists())

    def test_changed_and_new_candidates_are_retained_after_preview(self):
        path=self.fixture()
        plan=self.plan()
        (path/"payload").write_text("changed after review")
        new=self.home/"visualizations/new"
        new.mkdir();(new/"payload").write_text("not reviewed")
        self.assertEqual(self.invoke("--only", "storage", "--apply", "--approved-plan", str(plan), "--plan-sha256", m.sha256(plan)), 1)
        self.assertTrue(path.exists())
        self.assertTrue(new.exists())

    def test_fourteen_day_sessions_archive_and_never_expire(self):
        area=self.home/"sessions";area.mkdir()
        source=area/"rollout-old.jsonl";latest=area/"rollout-new.jsonl"
        source.write_text('{"payload":{"rate_limits":{}}}\n');latest.write_text('{}\n')
        os.utime(source,(self.now-15*86400,)*2)
        os.utime(latest,(self.now,)*2)
        events=[]
        record=lambda kind,path,action,**details:events.append((kind,str(path),action,details))
        m.retain_sessions(self.home,True,self.now,record)
        archive=self.home/"maintenance/session-archives/sessions/rollout-old.jsonl.gz"
        self.assertTrue(archive.exists())
        self.assertTrue(latest.exists())
        m.retain_sessions(self.home,True,self.now+500*86400,record)
        self.assertTrue(archive.exists())
        self.assertFalse(any(x[2]=="delete-expired" for x in events))

    def test_scheduler_cwd_keeps_expired_storage_and_session_candidates(self):
        path = self.fixture()
        area = self.home / "sessions"
        area.mkdir()
        old = area / "rollout-old.jsonl"
        old.write_text('{}\n')
        os.utime(old, (self.now - 15 * m.DAY,) * 2)
        latest = area / "rollout-latest.jsonl"
        latest.write_text('{}\n')
        os.utime(latest, (self.now,) * 2)
        for index, cwd in enumerate((self.home, self.home.parent)):
            with self.subTest(cwd=cwd), patch.object(Path, "cwd", return_value=cwd):
                plan = self.home / "maintenance" / ("scheduler-" + str(index) + ".json")
                self.assertEqual(self.invoke("--only", "all", "--plan", str(plan)), 0)
                entries = json.loads(plan.read_text())["mutations"]
                self.assertTrue(any(row["path"] == str(path) for row in entries))
                self.assertTrue(any(row["path"] == str(old) and row["action"] == "compress" for row in entries))

    def test_active_checkout_cwd_remains_pinned(self):
        checkout = self.home / "tmp" / "checkout"
        checkout.mkdir(parents=True)
        (checkout / "uncommitted").write_text("active task bytes")
        with patch.object(Path, "cwd", return_value=checkout):
            plan = self.plan()
        self.assertFalse(any(row["path"] == str(checkout) for row in json.loads(plan.read_text())["mutations"]))
        self.assertEqual((checkout / "uncommitted").read_text(), "active task bytes")

    def test_task_root_guard_rejects_home_repository_and_accepts_nested_project(self):
        subprocess.run(['git', '-C', str(self.home), 'init'], check=True, capture_output=True, timeout=15)
        nested = self.home / 'nested'
        nested.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'HOME_REPOSITORY_BLOCKED'):
            tasks.require_project_repository(nested, self.home)
        subprocess.run(['git', '-C', str(nested), 'init'], check=True, capture_output=True, timeout=15)
        self.assertEqual(tasks.require_project_repository(nested, self.home), nested)

    def test_task_root_guard_stops_before_command_or_scratch(self):
        with patch.object(tasks, 'require_project_repository', side_effect=RuntimeError('HOME_REPOSITORY_BLOCKED')), patch.object(tasks, 'command_main', return_value=0) as child:
            self.assertEqual(tasks.main(), 78)
            child.assert_not_called()

    def test_task_root_guard_handles_unicode_home(self):
        account = self.home / '\u5bb6\u76ee\u9304'
        account.mkdir()
        subprocess.run(['git', '-C', str(account), 'init'], check=True, capture_output=True, timeout=15)
        try:
            with self.assertRaisesRegex(RuntimeError, 'HOME_REPOSITORY_BLOCKED'):
                tasks.require_project_repository(account, account)
        except UnicodeError as error:
            self.fail('Git root decoding failed: ' + str(error))

    def test_task_root_guard_ignores_inherited_git_routing(self):
        subprocess.run(['git', '-C', str(self.home), 'init'], check=True, capture_output=True, timeout=15)
        independent = self.home.parent / 'independent-project'
        independent.mkdir()
        subprocess.run(['git', '-C', str(independent), 'init'], check=True, capture_output=True, timeout=15)
        overrides = {'GIT_DIR': str(independent / '.git'), 'GIT_WORK_TREE': str(independent)}
        with patch.dict(os.environ, overrides), patch.object(Path, 'cwd', return_value=self.home), patch.object(Path, 'home', return_value=self.home), patch.object(tasks, 'command_main', return_value=0) as child:
            self.assertEqual(tasks.main(), 78)
            child.assert_not_called()
            self.assertFalse((self.home / 'tmp/test-runs').exists())

    def test_deployed_rules_require_root_guard_and_project_handoff(self):
        with contextlib.redirect_stdout(io.StringIO()):
            installer.deploy(self.home, True, sys.executable)
        agents = (self.home / 'AGENTS.md').read_text()
        self.assertIn('git rev-parse --show-toplevel', agents)
        self.assertIn('progress, output paths, needed', agents)
        self.assertIn('worktrees and next step', agents)

    def test_recent_session_home_is_a_container_not_a_recursive_pin(self):
        path = self.fixture()
        with patch.object(m, 'recent_cwds', return_value={self.home}):
            plan = self.plan()
        self.assertTrue(any(row['path'] == str(path) for row in json.loads(plan.read_text())['mutations']))

    def test_session_cwd_is_recorded_without_resolving_protected_path(self):
        protected = self.home / '.claude/private'
        area = self.home / 'sessions'
        area.mkdir()
        timestamp = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        (area / 'rollout-active.jsonl').write_text(json.dumps({'timestamp': timestamp, 'payload': {'cwd': str(protected)}}) + '\n')
        original = Path.resolve
        def checked(path, *args, **kwargs):
            if path == protected:
                self.fail('session CWD resolution touched protected path')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'resolve', checked):
            self.assertIn(protected, m.recent_cwds(self.home, self.now))

    def test_recent_long_rollout_tail_stays_plain(self):
        area = self.home / "sessions"
        area.mkdir()
        source = area / "rollout-reopened.jsonl"
        timestamp = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        event = {"timestamp": timestamp, "payload": {"text": "x" * (70 * 1024)}}
        source.write_text(json.dumps(event) + '\n')
        os.utime(source, (self.now - 100 * m.DAY,) * 2)
        latest = area / "rollout-newer-mtime.jsonl"
        latest.write_text('{}\n')
        os.utime(latest, (self.now - m.DAY,) * 2)
        with patch.object(m.time, "time", return_value=self.now):
            self.assertAlmostEqual(m.rollout_activity(source), self.now, delta=0.001)
            m.retain_sessions(self.home, True, self.now, lambda *args, **kwargs: None)
        self.assertTrue(source.exists())
        self.assertFalse((self.home / "maintenance/session-archives/sessions/rollout-reopened.jsonl.gz").exists())

    def test_oversized_rollout_tail_is_retained_conservatively(self):
        source = self.home / "unbounded-event.jsonl"
        source.write_bytes(b'{"timestamp":"incomplete oversized event ' + b'x' * (70 * 1024))
        os.utime(source, (self.now - 100 * m.DAY,) * 2)
        with patch.object(m, "ROLLOUT_TAIL_LIMIT", 64 * 1024, create=True), patch.object(m.time, "time", return_value=self.now):
            self.assertGreaterEqual(m.rollout_activity(source), self.now)

    def test_local_rule_changes_require_another_preview(self):
        self.fixture();plan=self.plan()
        local=self.home/'maintenance/local.json'
        value=json.loads(local.read_text());value['storage']['owned_paths']=['changed-scope']
        local.write_text(json.dumps(value))
        with self.assertRaises(SystemExit):
            self.invoke('--only','storage','--apply','--approved-plan',str(plan),'--plan-sha256',m.sha256(plan))

    @unittest.skipIf(lifecycle.tomllib is None, 'MCP config edits require stdlib tomllib')
    def test_mcp_multiline_examples_and_quoted_tables_preserve_unrelated_config(self):
        for delimiter in ('\"\"\"', "'''"):
            with self.subTest(delimiter=delimiter):
                prefix = 'developer_instructions=' + delimiter + '\n[mcp_servers.codebase-memory-mcp]\ncommand="example-only"\n' + delimiter + '\n'
                server = '[mcp_servers."codebase-memory-mcp"]\ncommand="backend"\n[mcp_servers."codebase-memory-mcp".env]\nX="owned"\n'
                tail = '[mcp_servers.other]\ncommand="keep"\n'
                original = prefix + server + tail
                try:
                    changed, backend = installer.patch_mcp(original, self.home, sys.executable)
                except ValueError as error:
                    self.fail('valid multiline config must remain editable: ' + str(error))
                self.assertEqual(changed, prefix + tail)
                self.assertEqual(backend, 'removed-disabled-server')
                self.assertEqual(lifecycle.validate_toml(changed)['mcp_servers'], {'other':{'command':'keep'}})

    @unittest.skipIf(lifecycle.tomllib is None, 'MCP config edits require stdlib tomllib')
    def test_invalid_mcp_patch_result_blocks_deploy_before_any_write(self):
        prefix = 'developer_instructions=\"\"\"\n[mcp_servers.codebase-memory-mcp]\ncommand="example"\n\"\"\"\n'
        server = '[mcp_servers.codebase-memory-mcp]\ncommand="actual"\n'
        tail = '[other]\nvalue=7\n'
        original = prefix + server + tail
        config = self.home/'config.toml'
        config.write_text(original)
        bad = [prefix.index('[mcp_servers'), len(prefix), len(prefix)+len(server)]
        with patch.object(installer, 'toml_table_boundaries', return_value=bad), self.assertRaises(ValueError):
            installer.deploy(self.home, True, sys.executable)
        self.assertEqual(config.read_text(), original)
        self.assertFalse((self.home/'maintenance/runtime').exists())
        self.assertFalse((self.home/'maintenance/install-backups').exists())

    def test_mcp_parser_absence_blocks_install_before_any_write(self):
        config = self.home/'config.toml'
        original = '[mcp_servers.codebase-memory-mcp]\ncommand="keep-until-verified"\n'
        config.write_text(original)
        with patch.object(lifecycle, 'tomllib', None):
            try:
                installer.deploy(self.home, True, sys.executable)
            except ValueError as error:
                self.assertIn('requires-python-3.11', str(error))
            except Exception as error:
                self.fail('parser capability must fail explicitly: ' + str(error))
            else:
                self.fail('missing parser must stop before installation')
        self.assertEqual(config.read_text(), original)
        self.assertFalse((self.home/'maintenance/runtime').exists())
        self.assertFalse((self.home/'maintenance/install-backups').exists())

    @unittest.skipIf(lifecycle.tomllib is None, 'MCP config edits require stdlib tomllib')
    def test_mixed_dotted_mcp_definition_is_retained_before_deployment(self):
        original = 'mcp_servers.codebase-memory-mcp.command="backend"\n[mcp_servers.codebase-memory-mcp.env]\nX="owned"\n'
        config = self.home/'config.toml'
        config.write_text(original)
        with self.assertRaisesRegex(ValueError, 'MCP-definition-remains'):
            installer.deploy(self.home, True, sys.executable)
        self.assertEqual(config.read_text(), original)
        self.assertFalse((self.home/'maintenance/runtime').exists())
        self.assertFalse((self.home/'maintenance/install-backups').exists())

    @unittest.skipIf(lifecycle.tomllib is None, 'MCP config edits require stdlib tomllib')
    def test_inline_mcp_definition_is_retained_without_safe_table_edit(self):
        original = 'mcp_servers={"codebase-memory-mcp"={command="keep"}}\n'
        config = self.home/'config.toml'
        config.write_text(original)
        with self.assertRaisesRegex(ValueError, 'inline-definition'):
            installer.deploy(self.home, True, sys.executable)
        self.assertEqual(config.read_text(), original)
        self.assertFalse((self.home/'maintenance/runtime').exists())

    @unittest.skipIf(lifecycle.tomllib is None, 'MCP config edits require stdlib tomllib')
    def test_deployment_disables_mcp_and_installs_preview_hook_and_rule_once(self):
        original='[mcp_servers.codebase-memory-mcp]\ncommand="old.exe"\n[mcp_servers.codebase-memory-mcp.env]\nX="private"\n[other]\nvalue=7\n'
        (self.home/"config.toml").write_text(original)
        (self.home/"AGENTS.md").write_text("Existing user rule\n")
        with contextlib.redirect_stdout(io.StringIO()):
            installer.deploy(self.home,True,sys.executable)
            installer.deploy(self.home,True,sys.executable)
        self.assertEqual((self.home/"config.toml").read_text(),'[other]\nvalue=7\n')
        agents=(self.home/"AGENTS.md").read_text()
        self.assertTrue(agents.startswith("Existing user rule\n"))
        self.assertEqual(agents.count("<!-- codex-storage-lifecycle:start -->"),1)
        hooks=json.loads((self.home/"hooks.json").read_text())
        for event in ("Stop","SessionEnd"):
            clean=[h['command'] for group in hooks['hooks'][event] for h in group['hooks'] if 'test_run.py' in h['command']]
            self.assertEqual(len(clean),1)
            self.assertNotIn('--apply',clean[0])

    def test_heavy_execution_waits_for_the_existing_suite(self):
        marker=self.home/"unexpected-execution"
        script=Path(m.__file__).with_name("test_run.py")
        with workload.work_slot(self.home,heavy=True):
            result=subprocess.run([sys.executable,str(script),'--timeout','.3','--',sys.executable,'-c',"from pathlib import Path;Path(__import__('sys').argv[1]).touch()",str(marker)],env={**os.environ,'CODEX_HOME':str(self.home)},capture_output=True,timeout=10)
        self.assertNotEqual(result.returncode,0)
        self.assertIn(b'heavy workload slot wait expired',result.stderr)
        self.assertFalse(marker.exists())

    def test_configured_general_jobs_block_the_next_and_release_on_exit(self):
        code="import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from workload import work_slot\nwith work_slot(Path(sys.argv[2]),heavy=False,wait=.2):print('acquired')"
        command=[sys.executable,'-c',code,str(Path(workload.__file__).parent),str(self.home)]
        with contextlib.ExitStack() as leases:
            for _ in range(workload.GENERAL_JOB_SLOTS):leases.enter_context(workload.work_slot(self.home,heavy=False))
            blocked=subprocess.run(command,capture_output=True,timeout=10)
            self.assertNotEqual(blocked.returncode,0)
            self.assertIn(f'all {workload.GENERAL_JOB_SLOTS} general workload slots are occupied'.encode(),blocked.stderr)
        released=subprocess.run(command,capture_output=True,timeout=10)
        self.assertEqual(released.returncode,0,released.stderr)
        self.assertIn(b'acquired',released.stdout)

    @unittest.skipUnless(os.name == 'nt', 'Windows directory enumeration cache')
    def test_fresh_metadata_ignores_stale_directory_entry_attributes(self):
        import storage_lifecycle as storage
        path=self.fixture();guard=storage.Guard(self.home)
        expected=storage.snapshot(path,path.parent,guard).fingerprint
        real_scandir=os.scandir
        class Entry:
            def __init__(self,entry):self.entry,self.name=entry,entry.name
            def stat(self,**kwargs):
                info=self.entry.stat(**kwargs)
                fields={key:getattr(info,key) for key in ('st_mode','st_size','st_mtime','st_mtime_ns','st_ino','st_ctime')}
                fields.update(st_birthtime=getattr(info,'st_birthtime',0),st_size=info.st_size+1)
                return SimpleNamespace(**fields)
        @contextlib.contextmanager
        def cached(directory):
            with real_scandir(directory) as entries:yield [Entry(entry) for entry in entries]
        with patch.object(os,'scandir',side_effect=cached):
            actual=storage.snapshot(path,path.parent,guard).fingerprint
        self.assertEqual(actual,expected)

    def test_ancestor_pin_protects_old_output_children(self):
        path=self.fixture()
        (self.home/'visualizations/.codex-pin').touch()
        plan=self.plan()
        self.assertFalse(any(x['path']==str(path) for x in json.loads(plan.read_text())['mutations']))
        self.assertTrue(path.exists())


if __name__ == '__main__':unittest.main()
