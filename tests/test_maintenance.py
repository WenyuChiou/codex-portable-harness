import contextlib
import gzip
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import maintenance as m
import install_maintenance as installer


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / 'codex'
        self.home.mkdir()
        self.events = []
        self.record = lambda kind, path, action, **kw: self.events.append(dict(kind=kind, path=str(path), action=action, **kw))
        self.now = time.time()

    def age(self, path, days=4):
        for p in reversed(m.tree(path, self.home)):
            os.utime(p, (self.now-days*m.DAY, self.now-days*m.DAY))

    def repo(self):
        source = self.home / 'source'
        source.mkdir()
        m.run(['git', 'init', str(source)])
        m.git(source, 'config', 'user.name', 'Fixture')
        m.git(source, 'config', 'user.email', 'fixture@example.invalid')
        (source/'tracked').write_text('accepted')
        (source/'.gitignore').write_text('private/\n')
        m.git(source, 'add', 'tracked', '.gitignore')
        m.git(source, 'commit', '-m', 'fixture')
        worktree = self.home/'worktrees/batch/task'
        m.git(source, 'worktree', 'add', '--detach', str(worktree))
        self.age(worktree)
        self.age(source/'.git/worktrees')
        return source, worktree

    def rollout(self, area, name, days):
        p = self.home/area/('rollout-'+name+'.jsonl')
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({'payload': {'rate_limits': {'remaining': 1}}})+'\n')
        os.utime(p, (self.now-days*m.DAY, self.now-days*m.DAY))
        return p

    def test_worktree_dirty_untracked_and_pinned_are_retained(self):
        source, wt = self.repo()
        (wt/'new').write_text('untracked')
        self.age(wt)
        m.clean_worktrees(self.home, True, self.now, self.record, set())
        self.assertTrue(wt.exists())
        self.assertEqual(self.events[-1]['action'], 'dirty-report-only')
        (wt/'new').unlink()
        self.age(wt)
        m.clean_worktrees(self.home, True, self.now, self.record, {wt.resolve()})
        self.assertEqual(self.events[-1]['action'], 'active-or-pinned')

    def test_worktree_index_flags_retain_hidden_tracked_edits(self):
        source, wt = self.repo()
        for flag in ('skip-worktree', 'assume-unchanged'):
            with self.subTest(flag=flag):
                (wt/'tracked').write_text('accepted')
                m.git(wt, 'update-index', '--' + flag, '--', 'tracked')
                payload = 'irreplaceable hidden edits ' + flag
                (wt/'tracked').write_text(payload)
                self.assertEqual(m.git(wt, 'status', '--porcelain=v1'), b'')
                self.age(wt); self.age(source/'.git/worktrees')
                for apply in (False, True):
                    m.clean_worktrees(self.home, apply, self.now, self.record, set())
                    self.assertTrue(wt.exists(), self.events)
                    self.assertEqual(self.events[-1]['action'], 'dirty-report-only')
                    self.assertEqual((wt/'tracked').read_text(), payload)
                m.git(wt, 'update-index', '--no-' + flag, '--', 'tracked')

    def test_orphan_head_is_retained_before_remove_and_ignored_bytes_survive(self):
        source, wt = self.repo()
        (wt/'tracked').write_text('orphan commit')
        m.git(wt, 'add', 'tracked')
        m.git(wt, 'commit', '-m', 'orphan')
        head = m.git(wt, 'rev-parse', 'HEAD').decode().strip()
        (wt/'private').mkdir()
        (wt/'private/secret').write_bytes(b'irreplaceable\x00bytes')
        self.age(wt)
        self.age(source/'.git/worktrees')
        m.clean_worktrees(self.home, True, self.now, self.record, set())
        self.assertFalse(wt.exists(), self.events)
        self.assertEqual(m.git(source, 'rev-parse', 'refs/heads/maintenance-retained/'+head).decode().strip(), head)
        backups = list((self.home/'maintenance/preserved-worktrees').glob('*/files/private/secret'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b'irreplaceable\x00bytes')

    def test_ignored_filename_cannot_collide_with_preservation_manifest(self):
        source,wt=self.repo()
        with (wt/'.gitignore').open('a') as stream:stream.write('preservation.json\n')
        m.git(wt,'add','.gitignore')
        m.git(wt,'commit','-m','ignore fixture preservation file')
        payload=b'irreplaceable user preservation document'
        (wt/'preservation.json').write_bytes(payload)
        self.age(wt);self.age(source/'.git/worktrees')
        m.clean_worktrees(self.home,True,self.now,self.record,set())
        self.assertFalse(wt.exists(),self.events)
        root=Path(self.events[-1]['preserved'])
        manifest=json.loads((root/'preservation.json').read_text())
        self.assertEqual(manifest['data_directory'],'files')
        self.assertEqual((root/'files/preservation.json').read_bytes(),payload)

    def test_preview_recent_and_clone_report_do_not_remove(self):
        source, wt = self.repo()
        m.clean_worktrees(self.home, False, self.now, self.record, set())
        self.assertTrue(wt.exists())
        self.assertEqual(self.events[-1]['action'], 'eligible')
        os.utime(wt/'tracked', None)
        m.clean_worktrees(self.home, True, self.now, self.record, set())
        self.assertEqual(self.events[-1]['action'], 'recent')

    def test_git_failure_and_permission_failure_keep_candidate(self):
        _, wt = self.repo()
        with patch.object(m, 'clean_status', side_effect=TimeoutError('git timeout')):
            m.clean_worktrees(self.home, True, self.now, self.record, set())
        self.assertTrue(wt.exists())
        self.assertEqual(self.events[-1]['action'], 'blocked')

    def test_untracked_file_created_at_removal_is_kept_by_git(self):
        _, wt = self.repo()
        native_git=m.git
        def intervene(path,*args):
            if args[:2] == ('worktree','remove'):
                (wt/'new-user-work.txt').write_text('concurrent user work')
            return native_git(path,*args)
        with patch.object(m,'git',side_effect=intervene):
            m.clean_worktrees(self.home,True,self.now,self.record,set())
        self.assertEqual((wt/'new-user-work.txt').read_text(),'concurrent user work')
        self.assertEqual(self.events[-1]['action'],'blocked')

    def test_other_agent_gitdir_is_retained_before_git_runs(self):
        _, wt=self.repo()
        pointer='gitdir: '+str(self.home.parent/'.claude/private.git/worktrees/task')+'\n'
        with patch.object(Path,'read_text',return_value=pointer):
            with patch.object(m,'git',side_effect=AssertionError('Git must not read other-agent metadata')) as probe:
                m.clean_worktrees(self.home,True,self.now,self.record,set())
        probe.assert_not_called()
        self.assertTrue(wt.exists())
        self.assertEqual(self.events[-1]['action'],'blocked')

    def test_other_agent_commondir_is_retained_before_git_runs(self):
        _,wt=self.repo()
        _,metadata=m.metadata_hint(wt)
        original=Path.read_text
        def supplied_pointer(path,*args,**kwargs):
            if path == metadata/'commondir': return str(self.home.parent/'.claude/private.git')
            return original(path,*args,**kwargs)
        with patch.object(Path,'read_text',supplied_pointer):
            with patch.object(m,'git',side_effect=AssertionError('Git must not read other-agent common metadata')) as probe:
                m.clean_worktrees(self.home,True,self.now,self.record,set())
        probe.assert_not_called()
        self.assertTrue(wt.exists())
        self.assertEqual(self.events[-1]['action'],'blocked')

    def test_forbidden_lexical_metadata_is_rejected_before_resolution(self):
        for product in ('.claude','.gemini','.opencode'):
            with self.subTest(product=product):
                with patch.object(Path,'resolve',side_effect=AssertionError('forbidden path must not be resolved')) as resolve:
                    with self.assertRaises(ValueError):
                        m.allowed_metadata(self.home.parent/product/'repo.git')
                resolve.assert_not_called()
        with patch.object(Path,'resolve',return_value=self.home.parent/'.claude/repo.git'):
            with self.assertRaises(ValueError):
                m.allowed_metadata(self.home/'otherwise-permitted.git')

    def test_inherited_git_overrides_cannot_redirect_or_trace_cleanup(self):
        _,wt=self.repo()
        unrelated=self.home/'unrelated.git'
        m.run(['git','init','--bare',str(unrelated)])
        (unrelated/'config').write_text('! intentionally invalid configuration\n')
        trace=unrelated/'unexpected-trace.log'
        overrides={'GIT_DIR':str(unrelated),'GIT_COMMON_DIR':str(unrelated),
                   'GIT_CONFIG_COUNT':'1','GIT_CONFIG_KEY_0':'alias.fixture','GIT_CONFIG_VALUE_0':'bad',
                   'GIT_TRACE':str(trace)}
        with patch.dict(os.environ,overrides):
            m.clean_worktrees(self.home,True,self.now,self.record,set())
        self.assertFalse(wt.exists(),self.events)
        self.assertEqual(self.events[-1]['action'],'removed')
        self.assertFalse(trace.exists())

    def test_retention_keeps_newest_and_compresses_verified_content(self):
        newest = self.rollout('sessions', 'newest', 120)
        old = self.rollout('sessions', 'old', 121)
        m.retain_sessions(self.home, True, self.now, self.record)
        self.assertTrue(newest.exists())
        self.assertFalse(old.exists())
        raw = self.rollout('archived_sessions', 'forty', 40)
        newest2 = self.rollout('archived_sessions', 'today', 0)
        expected = raw.read_bytes()
        m.retain_sessions(self.home, True, self.now, self.record)
        target = self.home/'maintenance/session-archives/archived_sessions'/ (raw.name+'.gz')
        self.assertEqual(gzip.decompress(target.read_bytes()), expected)
        self.assertFalse(raw.exists())
        self.assertTrue(newest2.exists())
        self.assertAlmostEqual(target.stat().st_mtime, self.now-40*m.DAY, places=3)
        m.retain_sessions(self.home, True, self.now+51*m.DAY, self.record)
        self.assertTrue(target.exists())  # Session archives have no expiry.

    def test_archive_collision_keeps_source(self):
        raw = self.rollout('sessions','source',40)
        target = self.home/'maintenance/existing.gz'
        target.parent.mkdir()
        target.write_bytes(b'original')
        with self.assertRaisesRegex(ValueError, 'collision'):
            m.archive_one(raw,target,self.home)
        self.assertTrue(raw.exists())
        self.assertEqual(target.read_bytes(),b'original')

    def test_existing_native_compressed_archives_have_no_expiry(self):
        area=self.home/'archived_sessions'
        area.mkdir()
        for name,days in [('old',100),('latest',95),('recent',45)]:
            p=area/('rollout-'+name+'.jsonl.zst')
            p.write_bytes(b'preexisting native archive')
            os.utime(p,(self.now-days*m.DAY,)*2)
        m.retain_sessions(self.home,True,self.now,self.record)
        self.assertTrue((area/'rollout-old.jsonl.zst').exists())
        self.assertTrue((area/'rollout-latest.jsonl.zst').exists())
        self.assertTrue((area/'rollout-recent.jsonl.zst').exists())

    def test_recent_event_protects_rollout_with_stale_windows_mtime(self):
        from datetime import datetime, timezone
        p=self.rollout('sessions','reopened',100)
        timestamp=datetime.fromtimestamp(self.now,tz=timezone.utc).isoformat()
        p.write_text(json.dumps({'timestamp':timestamp,'payload':{'cwd':str(self.home/'worktrees/active')}})+'\n')
        os.utime(p,(self.now-100*m.DAY,)*2)
        self.rollout('sessions','other',1)
        m.retain_sessions(self.home,True,self.now,self.record)
        self.assertTrue(p.exists())
        self.assertIn((self.home/'worktrees/active').resolve(),m.recent_cwds(self.home,self.now))

    def test_tmp_child_activity_blocks_whole_directory_and_old_files_removed(self):
        old = self.home/'tmp/old'
        old.mkdir(parents=True)
        (old/'data').write_text('temp')
        self.age(old, 8)
        mixed = self.home/'tmp/mixed'
        mixed.mkdir()
        (mixed/'recent').write_text('still active')
        os.utime(mixed,(self.now-9*m.DAY,)*2)
        m.clean_tmp(self.home,True,self.now,self.record)
        self.assertFalse(old.exists())
        self.assertTrue((mixed/'recent').exists())

    def test_symlink_escape_is_rejected(self):
        target = Path(self.temp.name)/'outside'
        target.mkdir()
        link = self.home/'tmp'
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest('symlink creation requires Windows developer mode')
        with self.assertRaises(ValueError):
            m.clean_tmp(self.home,True,self.now,self.record)

    @unittest.skipUnless(os.name == 'nt', 'Windows junction safety')
    def test_windows_junction_escape_is_rejected(self):
        target=Path(self.temp.name)/'outside-junction'
        target.mkdir()
        sentinel=target/'preserve'
        sentinel.write_text('keep')
        link=self.home/'tmp'
        result=subprocess.run(['cmd','/c','mklink','/J',str(link),str(target)],capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        with self.assertRaises(ValueError):
            m.clean_tmp(self.home,True,self.now,self.record)
        self.assertEqual(sentinel.read_text(),'keep')

    def test_mcp_patch_preserves_other_settings_and_is_idempotent(self):
        original = '[mcp_servers.codebase-memory-mcp]\ncommand = "backend.exe"\n\n[other]\nvalue = 7\n'
        changed, backend = installer.patch_mcp(original,self.home,sys.executable)
        self.assertEqual(backend,'removed-disabled-server')
        self.assertIn('[other]\nvalue = 7\n',changed)
        self.assertNotIn('codebase-memory-mcp',changed)
        self.assertEqual(installer.patch_mcp(changed,self.home,sys.executable)[0],changed)

    def test_installer_preserves_hooks_and_rejects_user_runtime_edit(self):
        (self.home/'hooks.json').write_text('{"hooks":{"Stop":[{"hooks":[{"command":"existing"}]}]}}')
        with contextlib.redirect_stdout(io.StringIO()):
            installer.deploy(self.home,True,sys.executable)
            installer.deploy(self.home,True,sys.executable)
        data=json.loads((self.home/'hooks.json').read_text())
        self.assertEqual(len(data['hooks']['Stop']),2)
        self.assertEqual(data['hooks']['Stop'][0]['hooks'][0]['command'],'existing')
        (self.home/'maintenance/runtime/maintenance.py').write_text('user edit')
        with self.assertRaisesRegex(ValueError,'conflict'):
            installer.deploy(self.home,True,sys.executable)

    def test_command_timeout_is_not_success(self):
        with self.assertRaises(TimeoutError):
            m.run([sys.executable,'-c','import time; time.sleep(30)'],timeout=.1)

    @unittest.skipUnless(os.name == 'nt', 'Windows current-user DACL repair')
    def test_native_acl_repair_grants_current_user_without_sacl_privilege(self):
        executable='powershell.exe'
        sid=subprocess.check_output([executable,'-NoProfile','-Command','[Security.Principal.WindowsIdentity]::GetCurrent().User.Value'],text=True).strip()
        target=self.home/'cache'
        target.mkdir()
        (target/'keep.txt').write_text('accepted bytes')
        m.run(['icacls.exe',str(target),'/inheritance:r','/grant:r','*'+sid+':(OI)(CI)RX','/Q'])
        try:
            result=subprocess.run([executable,'-NoProfile','-ExecutionPolicy','Bypass','-File',str(ROOT/'scripts/repair_maintenance_acl.ps1'),'-Apply','-Path',str(target)],
                                  capture_output=True,env={**os.environ,'CODEX_HOME':str(self.home)},timeout=30)
            self.assertEqual(result.returncode,0,(result.stdout+result.stderr).decode(errors='replace'))
            self.assertEqual((target/'keep.txt').read_text(),'accepted bytes')
            (target/'writable.txt').write_text('grant verified')
            self.assertTrue(list((self.home/'maintenance/acl-backups').glob('*.jsonl')))
        finally:
            m.run(['icacls.exe',str(target),'/grant','*'+sid+':(OI)(CI)F','/Q'])


if __name__ == '__main__':
    unittest.main()
