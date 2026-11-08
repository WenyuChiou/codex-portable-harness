"""Storage lifecycle behavior against disposable files and local Git repositories."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
try:
    import tomllib
except ImportError:
    tomllib = None
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import maintenance as maintenance
import storage_lifecycle as lifecycle


class StorageLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Path(self.temporary.name).resolve()
        self.home = self.fixture / "codex"
        self.home.mkdir()
        # Creation timestamps are old relative to this clock on Windows too.
        self.now = time.time() + 200 * lifecycle.DAY
        self.events = []

    def record(self, kind, path, action, **details):
        self.events.append(dict(kind=kind, path=str(path), action=action, **details))

    def write(self, path, contents=b"fixture payload"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        return path

    def age(self, path, days=100):
        stamp = self.now - days * lifecycle.DAY
        if path.is_dir():
            for directory, dirs, files in os.walk(path, topdown=False):
                for name in files + dirs:
                    os.utime(Path(directory) / name, (stamp, stamp))
        os.utime(path, (stamp, stamp))

    def sweep(self, config=None, apply=True, now=None):
        config = config or {}
        self.events.clear()
        guard = lifecycle.Guard(self.home, config.get("storage", {}).get("pinned", []))
        lifecycle.sweep(self.home, apply, self.now if now is None else now,
                        self.record, guard, config, maintenance.git)

    def event(self, path):
        matches = [event for event in self.events if event["path"] == str(path)]
        self.assertTrue(matches, (path, self.events))
        return matches[-1]

    def assert_kept(self, path):
        self.assertTrue(path.exists(), self.events)
        event = self.event(path)
        self.assertIn(event["action"], {"retained", "blocked"}, event)
        return event

    def clone(self, name="task"):
        source = self.home / "repositories" / (name + "-backup")
        source.mkdir(parents=True)
        maintenance.git(source, "init", "--initial-branch=main")
        maintenance.git(source, "config", "core.longpaths", "true")
        maintenance.git(source, "config", "core.autocrlf", "false")
        maintenance.git(source, "config", "user.name", "Fixture")
        maintenance.git(source, "config", "user.email", "fixture@example.invalid")
        self.write(source / "tracked", b"accepted committed outcome\n")
        self.write(source / ".gitignore", b"private/\n__pycache__/\n")
        maintenance.git(source, "add", "tracked", ".gitignore")
        maintenance.git(source, "commit", "-m", "fixture accepted outcome")
        root = self.home / ("clones-" + name)
        root.mkdir()
        candidate = root / "task"
        maintenance.git(self.home, "-c", "core.longpaths=true", "clone", "--no-local",
                        "--config", "core.longpaths=true", "--config", "core.autocrlf=false",
                        str(source), str(candidate))
        maintenance.git(candidate, "config", "user.name", "Fixture")
        maintenance.git(candidate, "config", "user.email", "fixture@example.invalid")
        config = {"storage": {"clone_roots": [str(root)],
                              "clone_backups": {str(candidate): str(source)}}}
        return source, candidate, config

    def test_expired_outputs_removed_only_after_apply(self):
        paths = [self.write(self.home / relative / "result.bin") for relative in (
            "tmp/task", ".tmp/task", "visualizations/2020/01/01/task",
            "generated_images/2020-01-01/task", "harness-runs/task",
            "portable-harness/artifacts/task", "maintenance/logs/task")]
        for path in paths:
            self.age(path.parent)
        self.sweep(apply=False)
        for path in paths:
            self.assertTrue(path.exists())
            self.assertEqual(self.event(path.parent)["action"], "delete-expired")
        self.sweep()
        for path in paths:
            self.assertFalse(path.parent.exists(), self.events)
            self.assertEqual(self.event(path.parent)["action"], "removed")

    def test_recent_descendant_and_three_day_boundary_are_retained(self):
        for label, seconds in (("recent", 2 * lifecycle.DAY),
                               ("boundary", 3 * lifecycle.DAY)):
            path = self.write(self.home / "tmp" / label / "nested" / "output")
            self.age(path.parents[1])
            stamp = self.now - seconds
            os.utime(path, (stamp, stamp))
        self.sweep()
        for label in ("recent", "boundary"):
            path = self.home / "tmp" / label
            self.assertEqual(self.assert_kept(path)["reason"], "recent-within-three-days")

    def test_new_creation_time_retains_backdated_file(self):
        path = self.write(self.home / "tmp" / "newly-created.bin")
        if not hasattr(path.stat(), "st_birthtime"):
            self.skipTest("filesystem creation time is unavailable")
        actual_now = time.time()
        stamp = actual_now - 100 * lifecycle.DAY
        os.utime(path, (stamp, stamp))
        self.sweep(now=actual_now)
        self.assertEqual(self.assert_kept(path)["reason"], "recent-within-three-days")

    def test_recent_metadata_retains_backdated_birth_and_mtime(self):
        path=self.write(self.home/'tmp/backdated.bin')
        now=time.time();old=now-100*lifecycle.DAY
        os.utime(path,(old,old))
        original=os.lstat
        def metadata(item,*args,**kwargs):
            info=original(item,*args,**kwargs)
            lexical=Path(os.fsdecode(item).removeprefix('\\\\?\\'))
            if lifecycle.absolute(lexical)!=path:return info
            fields={key:getattr(info,key) for key in ('st_mode','st_size','st_mtime','st_mtime_ns','st_ino')}
            fields.update(st_birthtime=old,st_ctime=now,st_file_attributes=getattr(info,'st_file_attributes',0))
            return SimpleNamespace(**fields)
        with patch.object(os,'lstat',side_effect=metadata):
            self.sweep(now=now)
        self.assertEqual(self.assert_kept(path)['reason'],'recent-within-three-days')

    def test_markers_config_pins_and_accepted_receipts_retain_outputs(self):
        candidates = []
        for marker in sorted(lifecycle.PINS):
            path = self.home / "tmp" / marker.lstrip(".")
            self.write(path / "outcome")
            self.write(path / "nested" / marker, b"")
            self.age(path)
            candidates.append(path)
        pinned = self.home / "tmp" / "configured-pin"
        self.write(pinned / "outcome")
        self.age(pinned)
        candidates.append(pinned)
        accepted = self.home / "tmp" / "accepted"
        self.write(accepted / "outcome")
        self.write(accepted / ".lifecycle.json", json.dumps({"accepted": True}).encode())
        self.age(accepted)
        candidates.append(accepted)
        self.sweep({"storage": {"pinned": [str(pinned / "outcome")]}})
        for path in candidates:
            self.assert_kept(path)
        self.assertEqual(self.event(accepted)["reason"], "pinned-accepted-or-running")

    def test_protected_paths_rejected_before_filesystem_access(self):
        guard = lifecycle.Guard(self.home)
        protected = [self.home / name / "unread" for name in
                     (".claude", "Backup-2026-11-08", "ab-kept-home", ".ollama")]
        protected.append(Path.home() / ".git" / "unread")
        for path in protected:
            with self.subTest(path=path):
                with patch.object(os, "lstat", side_effect=AssertionError("filesystem access")) as probe:
                    with patch.object(Path, "resolve", side_effect=AssertionError("path resolution")) as resolve:
                        with self.assertRaisesRegex(ValueError, "protected-path"):
                            guard.check(path, self.home, allow_missing=True)
                probe.assert_not_called()
                resolve.assert_not_called()

    def test_recursive_discovery_rejects_protected_descendants_before_access(self):
        root=self.home/'clone-discovery'
        paths=[root/name for name in ('.claude','.trash-claude','Backup-2026-11-08','ab-kept-home','.ollama')]
        for path in paths:self.write(path/'necessary',b'fixture data')
        guard=lifecycle.Guard(self.home)
        real_lstat,real_scandir=os.lstat,os.scandir
        def checked_lstat(path,*args,**kwargs):
            lexical=Path(os.fsdecode(path).removeprefix('\\\\?\\'))
            if guard.forbidden(lexical):self.fail('protected descendant metadata accessed')
            return real_lstat(path,*args,**kwargs)
        def checked_scandir(path):
            lexical=Path(os.fsdecode(path).removeprefix('\\\\?\\'))
            if guard.forbidden(lexical):self.fail('protected descendant enumerated')
            return real_scandir(path)
        with patch.object(os,'lstat',side_effect=checked_lstat),patch.object(os,'scandir',side_effect=checked_scandir):
            self.assertEqual(set(lifecycle.repository_units(root,guard)),set(paths))

    def test_snapshot_rejects_protected_child_before_stat(self):
        root=self.home/'tmp/task'
        self.write(root/'.claude/necessary',b'fixture data')
        guard=lifecycle.Guard(self.home)
        original=os.lstat
        def checked(path,*args,**kwargs):
            if guard.forbidden(Path(os.fsdecode(path).removeprefix('\\\\?\\'))):
                self.fail('protected child stat was reached')
            return original(path,*args,**kwargs)
        with patch.object(os,'lstat',side_effect=checked):
            with self.assertRaisesRegex(ValueError,'protected-descendant'):
                lifecycle.snapshot(root,root.parent,guard)

    def test_unowned_shared_temp_is_retained_without_payload_scan(self):
        shared = self.fixture / "shared-temp"
        payload = self.write(shared / "another-user-task" / "necessary.bin", b"unowned necessary bytes")
        self.age(payload.parent)
        original_scandir = os.scandir

        def scoped_scandir(path):
            lexical = Path(os.fsdecode(path).removeprefix("\\\\?\\"))
            if lifecycle.absolute(lexical) == payload.parent:
                raise AssertionError("unowned task payload was scanned")
            return original_scandir(path)

        with patch.object(os, "scandir", side_effect=scoped_scandir):
            self.sweep({"storage": {"legacy_roots": [str(shared)]}})
        self.assertEqual(payload.read_bytes(), b"unowned necessary bytes")
        decisions = [e for e in self.events if e["kind"] == "legacy-scratch"]
        self.assertTrue(decisions)
        self.assertTrue(all(e["action"] == "retained" for e in decisions), decisions)

    def test_registered_ownership_does_not_authorize_sibling_removal(self):
        root = self.fixture / "shared-temp"
        owned = self.write(root / "owned" / "result").parent
        sibling = self.write(root / "unowned" / "necessary", b"keep sibling").parent
        self.age(owned)
        self.age(sibling)
        self.sweep({"storage": {"legacy_roots": [str(root)], "owned_paths": [str(owned)]}})
        self.assertFalse(owned.exists(), self.events)
        self.assertEqual((sibling / "necessary").read_bytes(), b"keep sibling")

    def test_unknown_state_and_referenced_or_active_checkpoints_survive(self):
        unknown = self.write(self.home / "portable-harness/state" / "unknown.json", b"{}")
        payloads = {"accepted.json": {"status": "completed", "accepted_evidence": ["accepted-result"]},
                    "nested.json": {"task": {"status": "complete", "evidence_refs": ["result"]}},
                    "running.json": {"status": "running"},
                    "unknown.json": {"unexpected": True},
                    "complete.json": {"status": "completed"}}
        paths = {}
        for name, value in payloads.items():
            paths[name] = self.write(self.home / "portable-harness/checkpoints" / name,
                                     json.dumps(value).encode())
            self.age(paths[name])
        self.age(unknown)
        self.sweep()
        self.assert_kept(unknown)
        for name, path in paths.items():
            if name == "complete.json":
                self.assertFalse(path.exists(), self.events)
            else:
                self.assert_kept(path)

    def test_clean_clone_requires_external_reachable_commit_proof(self):
        source, candidate, config = self.clone()
        head = maintenance.git(candidate, "rev-parse", "HEAD")
        self.age(candidate)
        self.sweep({"storage": {"clone_roots": config["storage"]["clone_roots"]}})
        self.assert_kept(candidate)
        self.sweep(config)
        self.assertFalse(candidate.exists(), self.events)
        self.assertEqual(maintenance.git(source, "rev-parse", "HEAD"), head)
        self.assertEqual((source / "tracked").read_bytes(), b"accepted committed outcome\n")

    def test_clean_clone_proof_rejects_missing_external_backup(self):
        _,candidate,_=self.clone()
        reason=lifecycle.clone_retained_elsewhere(candidate,None,lifecycle.Guard(self.home),maintenance.git)
        self.assertEqual(reason,'external-commit-backup-required')

    def test_known_retained_candidates_skip_payload_scans(self):
        pinned=self.write(self.home/'visualizations/pinned/necessary').parent
        recent=self.write(self.home/'tmp/recent/necessary').parent
        self.age(pinned)
        os.utime(recent,(self.now-lifecycle.DAY,)*2)
        guard=lifecycle.Guard(self.home,[pinned])
        with patch.object(lifecycle,'snapshot',side_effect=AssertionError('retained payload was scanned')):
            lifecycle.sweep(self.home,False,self.now,self.record,guard,{},maintenance.git)
        self.assertTrue(pinned.exists());self.assertTrue(recent.exists())

    def test_unbacked_clone_skips_payload_and_git_probes(self):
        _,candidate,config=self.clone();self.age(candidate)
        del config['storage']['clone_backups']
        with patch.object(lifecycle,'snapshot',side_effect=AssertionError('unbacked clone scanned')):
            self.sweep(config)
        self.assertEqual(self.assert_kept(candidate)['reason'],'external-commit-backup-required')

    def test_clone_discovery_skips_individual_task_logs(self):
        _,candidate,_=self.clone()
        root=candidate.parent
        for number in range(30):self.write(root/'task-logs'/f'{number}.log')
        found=list(lifecycle.repository_units(root,lifecycle.Guard(self.home)))
        self.assertIn(candidate,found)
        self.assertEqual(found,[candidate])

    def test_clone_retention_boundary_is_fourteen_days(self):
        _,candidate,config=self.clone()
        self.age(candidate,14);self.sweep(config)
        self.assert_kept(candidate)
        self.age(candidate,15);self.sweep(config)
        self.assertFalse(candidate.exists(),self.events)

    def test_unpublished_clone_retained_when_backup_lacks_commit(self):
        source, candidate, config = self.clone()
        maintenance.git(candidate, "commit", "--allow-empty", "-m", "unpublished fixture work")
        self.assertNotEqual(maintenance.git(source, "rev-parse", "HEAD"),
                            maintenance.git(candidate, "rev-parse", "HEAD"))
        self.age(candidate)
        self.sweep(config)
        self.assert_kept(candidate)

    def test_backup_objects_linked_into_candidate_are_not_independent(self):
        source,candidate,config=self.clone()
        objects=source/'.git/objects'
        for directory,dirs,files in os.walk(objects):
            for name in files:os.chmod(Path(directory)/name,stat.S_IWRITE|stat.S_IREAD)
        shutil.rmtree(objects)
        target=candidate/'.git/objects'
        if os.name=='nt':
            result=subprocess.run(['cmd','/d','/c','mklink','/J',str(objects),str(target)],capture_output=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            self.addCleanup(lambda:os.rmdir(objects) if os.path.lexists(objects) else None)
        else:
            objects.symlink_to(target,target_is_directory=True)
            self.addCleanup(lambda:objects.unlink() if objects.is_symlink() else None)
        self.age(candidate)
        nested=source/'.git/objects/info/alternates'
        original=os.path.lexists
        def checked_probe(path):
            if lifecycle.absolute(path)==nested:self.fail('redirected backup metadata probed before validation')
            return original(path)
        with patch.object(os.path,'lexists',side_effect=checked_probe):
            self.sweep(config)
        self.assert_kept(candidate)
        self.assertEqual(maintenance.git(source,'cat-file','-t','HEAD').strip(),b'commit')

    def test_dirty_clone_retains_uncommitted_work(self):
        _, candidate, config = self.clone()
        self.write(candidate / "tracked", b"uncommitted necessary work")
        self.age(candidate)
        self.sweep(config)
        self.assertEqual(self.assert_kept(candidate)["reason"], "dirty-without-verified-backup")
        self.assertEqual((candidate / "tracked").read_bytes(), b"uncommitted necessary work")

    def test_ignored_necessary_bytes_without_restoration_copy_retain_clone(self):
        source, candidate, config = self.clone()
        ignored = self.write(candidate / "private" / "necessary.bin", b"unique ignored payload\x00")
        self.assertFalse((source / "private" / "necessary.bin").exists())
        self.assertEqual(maintenance.git(candidate, "status", "--porcelain"), b"")
        self.age(candidate)
        self.sweep(config)
        self.assertEqual(self.assert_kept(candidate)["reason"], "ignored-payload-without-verified-backup")
        self.assertEqual(ignored.read_bytes(), b"unique ignored payload\x00")

    def test_preservation_capacity_keeps_existing_bytes_and_blocks_overflow(self):
        kept = self.write(self.home / "maintenance/preserved-worktrees" / "one" / "needed.bin", b"123456")
        lifecycle.preserve_capacity(self.home, 4, cap=10)
        with self.assertRaisesRegex(ValueError, "preservation-cap-reached-source-retained"):
            lifecycle.preserve_capacity(self.home, 5, cap=10)
        self.assertEqual(kept.read_bytes(), b"123456")

    def test_duplicate_proof_compares_necessary_bytes_not_sizes_or_manifest(self):
        source = self.home / "original"
        target = self.home / "copy"
        for root in (source, target):
            self.write(root / "manifest.json", b'{"files":["needed.bin"]}')
        self.write(source / "needed.bin", b"ABC")
        self.write(target / "needed.bin", b"XYZ")
        guard = lifecycle.Guard(self.home)
        self.assertFalse(lifecycle.duplicate_verified(source, target, guard))
        self.write(target / "needed.bin", b"ABC")
        self.assertTrue(lifecycle.duplicate_verified(source, target, guard))
        (target / "needed.bin").unlink()
        self.assertFalse(lifecycle.duplicate_verified(source, target, guard))
        self.assertFalse(lifecycle.duplicate_verified(source, source, guard))

    def test_preserved_copy_removed_only_with_complete_duplicate(self):
        original = self.home / "maintenance/preserved-worktrees" / "accepted"
        duplicate = self.home / "verified-preservation-copy"
        payload = self.write(original / "needed.bin", b"irreplaceable preserved bytes")
        receipt = json.dumps({"redundant_copy": str(duplicate)}).encode()
        self.write(original / ".lifecycle.json", receipt)
        self.age(original)
        self.sweep()
        self.assert_kept(original)
        self.assertEqual(payload.read_bytes(), b"irreplaceable preserved bytes")
        shutil.copytree(original, duplicate)
        self.age(original)
        self.sweep()
        self.assertFalse(original.exists(), self.events)
        self.assertEqual((duplicate / "needed.bin").read_bytes(), b"irreplaceable preserved bytes")

    def test_missing_project_requires_three_days_and_preserves_unrelated_toml(self):
        missing = self.home / "gone project"
        existing = self.home / "existing project"
        existing.mkdir()
        missing_section = '[projects.' + json.dumps(str(missing)) + ']\r\ntrust_level = "trusted"\r\n\r\n'
        unrelated = '# Keep exact comments and CRLF.\r\nmodel = "fixture"\r\n\r\n'
        unrelated += '[projects.' + json.dumps(str(existing)) + ']\r\ntrust_level = "trusted"\r\n\r\n'
        tail = '[features]\r\ncustom = true\r\n# trailing comment\r\n'
        raw = (unrelated + missing_section + tail).encode()
        config = self.write(self.home / "config.toml", raw)
        guard = lifecycle.Guard(self.home)
        for offset in (0, 2 * lifecycle.DAY, 3 * lifecycle.DAY):
            lifecycle.prune_projects(self.home, True, self.now + offset, self.record, guard)
            self.assertEqual(config.read_bytes(), raw)
        ledger = json.loads((self.home / "maintenance/missing-projects.json").read_text())
        self.assertEqual(ledger[str(missing)]["first_missing"], self.now)
        lifecycle.prune_projects(self.home, True, self.now + 3 * lifecycle.DAY + 1,
                                 self.record, guard)
        expected = (unrelated + tail).encode()
        self.assertEqual(config.read_bytes(), expected)
        if tomllib is not None:
            self.assertEqual(tomllib.loads(expected.decode())["features"], {"custom": True})
        backups = list((self.home / "maintenance/config-backups").glob("*.toml"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), raw)

    def test_changed_project_section_restarts_missing_observation(self):
        missing = self.home / "gone"
        initial = ('[projects.' + json.dumps(str(missing)) + ']\ntrust_level = "trusted"\n').encode()
        config = self.write(self.home / "config.toml", initial)
        guard = lifecycle.Guard(self.home)
        lifecycle.prune_projects(self.home, True, self.now, self.record, guard)
        changed = initial.replace(b"trusted", b"untrusted")
        config.write_bytes(changed)
        lifecycle.prune_projects(self.home, True, self.now + 4 * lifecycle.DAY, self.record, guard)
        self.assertEqual(config.read_bytes(), changed)
        ledger = json.loads((self.home / "maintenance/missing-projects.json").read_text())
        self.assertEqual(ledger[str(missing)]["first_missing"], self.now + 4 * lifecycle.DAY)

    def link_directory(self, link, target):
        if os.name == "nt":
            result = subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.addCleanup(lambda: os.rmdir(link) if os.path.lexists(link) else None)
        else:
            link.symlink_to(target, target_is_directory=True)
            self.addCleanup(lambda: link.unlink() if link.is_symlink() else None)

    def test_config_backup_reparse_keeps_config_and_external_bytes(self):
        missing = self.home / "gone"
        raw = ('[projects.' + json.dumps(str(missing)) + ']\ntrust_level="trusted"\n').encode()
        config = self.write(self.home / "config.toml", raw)
        guard = lifecycle.Guard(self.home)
        lifecycle.prune_projects(self.home, False, self.now, self.record, guard)
        outside = self.fixture / "external-backups"
        outside.mkdir()
        sentinel = self.write(outside / "keep", b"original target bytes")
        self.link_directory(self.home / "maintenance/config-backups", outside)
        with self.assertRaisesRegex(ValueError, "reparse-point"):
            lifecycle.prune_projects(self.home, True, self.now + 4 * lifecycle.DAY, self.record, guard)
        self.assertEqual(config.read_bytes(), raw)
        self.assertEqual(sentinel.read_bytes(), b"original target bytes")
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["keep"])

    def test_missing_project_ledger_ancestry_rejected_before_write(self):
        missing = self.home / "gone"
        raw = ('[projects.' + json.dumps(str(missing)) + ']\ntrust_level="trusted"\n').encode()
        config = self.write(self.home / "config.toml", raw)
        outside = self.fixture / "external-ledger"
        outside.mkdir()
        self.link_directory(self.home / "maintenance", outside)
        with self.assertRaisesRegex(ValueError, "reparse-point"):
            lifecycle.prune_projects(self.home, False, self.now, self.record, lifecycle.Guard(self.home))
        self.assertEqual(config.read_bytes(), raw)
        self.assertEqual(list(outside.iterdir()), [])

    def test_changed_or_newly_pinned_candidate_cannot_be_removed(self):
        root = self.home / "tmp"
        guard = lifecycle.Guard(self.home)
        for label in ("changed", "pinned"):
            candidate = self.write(root / label / "necessary").parent
            before = lifecycle.snapshot(candidate, root, guard)
            addition = candidate / ("new-bytes" if label == "changed" else ".codex-pin")
            self.write(addition, b"keep")
            with self.assertRaisesRegex(ValueError, "candidate-changed-or-pinned"):
                lifecycle.remove(candidate, root, guard, before.fingerprint)
            self.assertEqual(addition.read_bytes(), b"keep")

    def test_long_path_readonly_file_removed_inside_verified_candidate(self):
        candidate = self.home / "tmp" / "long-readonly"
        deep = candidate
        while len(str(deep)) < 330:
            deep /= "long-fixture-component"
        os.makedirs(lifecycle.native(deep))
        payload = deep / "readonly.bin"
        with open(lifecycle.native(payload), "wb") as stream:
            stream.write(b"rebuildable fixture output")
        os.chmod(lifecycle.native(payload), stat.S_IREAD)
        guard = lifecycle.Guard(self.home)
        before = lifecycle.snapshot(candidate, candidate.parent, guard)
        try:
            lifecycle.remove(candidate, candidate.parent, guard, before.fingerprint)
        except (OSError, ValueError) as exc:
            self.fail('verified readonly long-path fixture must be removable: '+str(exc))
        self.assertFalse(os.path.lexists(lifecycle.native(candidate)))

    def test_symlink_candidate_keeps_external_fixture_target(self):
        target = self.write(self.fixture / "outside-home" / "necessary.bin", b"external fixture bytes")
        link = self.home / "tmp" / "linked"
        link.parent.mkdir()
        try:
            link.symlink_to(target.parent, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("directory symlinks unavailable: " + str(exc))
        self.addCleanup(lambda: link.unlink() if link.is_symlink() else None)
        self.sweep()
        self.assertEqual(target.read_bytes(), b"external fixture bytes")
        self.assertTrue(link.is_symlink())
        self.assertIn("reparse-point", self.event(link).get("error", ""))

    @unittest.skipUnless(os.name == "nt", "Windows junction fixture")
    def test_real_windows_junction_keeps_external_fixture_target(self):
        target = self.write(self.fixture / "junction-target" / "necessary.bin", b"junction target bytes")
        link = self.home / "tmp" / "junction"
        link.parent.mkdir()
        result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(target.parent)],
                                capture_output=True, text=True, timeout=15)
        if result.returncode:
            self.skipTest("junction creation unavailable: " + result.stdout + result.stderr)
        self.addCleanup(lambda: os.rmdir(link) if os.path.lexists(link) else None)
        self.assertTrue(os.lstat(link).st_file_attributes & 0x400)
        self.sweep()
        self.assertEqual(target.read_bytes(), b"junction target bytes")
        self.assertTrue(os.path.lexists(link))
        self.assertIn("reparse-point", self.event(link).get("error", ""))


if __name__ == "__main__":
    unittest.main()
