"""Counterfactual storage checks in disposable copies; real host data is excluded."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
L = "test_storage_lifecycle.StorageLifecycleTests."
E = "test_storage_entrypoints.StorageEntrypoints."
T = "test_test_run.TestRunTests."

# Exact replacements fail closed when source changes. Each selected case first
# passes unchanged, then must produce an assertion failure with the fix removed.
CASES = [
    ("expiry", "storage_lifecycle.py", "elif now - info.latest <= days * DAY:", "elif True:", L+"test_expired_outputs_removed_only_after_apply"),
    ("recent", "storage_lifecycle.py", "elif now - info.latest <= 3 * DAY:", "elif False:", L+"test_recent_descendant_and_three_day_boundary_are_retained"),
    ("creation-time", "storage_lifecycle.py", 'return max(info.st_mtime, getattr(info, "st_birthtime", 0.), info.st_ctime)', 'return info.st_mtime', L+"test_new_creation_time_retains_backdated_file"),
    ("metadata-change-time", "storage_lifecycle.py", 'return max(info.st_mtime, getattr(info, "st_birthtime", 0.), info.st_ctime)', 'return max(info.st_mtime, getattr(info, "st_birthtime", 0.))', L+"test_recent_metadata_retains_backdated_birth_and_mtime"),
    ("pins", "storage_lifecycle.py", "return Snapshot(digest.hexdigest(), latest, size, count, pinned, repositories)", "return Snapshot(digest.hexdigest(), latest, size, count, False, repositories)", L+"test_markers_config_pins_and_accepted_receipts_retain_outputs"),
    ("ancestor-pin", "storage_lifecycle.py", "for ancestor in path.parents:", "for ancestor in ():", E+"test_ancestor_pin_protects_old_output_children"),
    ("protected-roots", "storage_lifecycle.py", "if self.forbidden(path) or self.forbidden(root):", "if False:", L+"test_protected_paths_rejected_before_filesystem_access"),
    ("shared-temp-ownership", "storage_lifecycle.py", 'if not implicit_owner and path not in owned and value.get("owner") != OWNER and kind != "task-clone":', 'if False:', L+"test_unowned_shared_temp_is_retained_without_payload_scan"),
    ("reparse", "storage_lifecycle.py", 'if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:', 'if False:', L+("test_real_windows_junction_keeps_external_fixture_target" if os.name == "nt" else "test_symlink_candidate_keeps_external_fixture_target")),
    ("readonly", "storage_lifecycle.py", "os.chmod(name, info.st_mode | stat.S_IWUSR)", "pass  # removed readonly recovery", L+"test_long_path_readonly_file_removed_inside_verified_candidate"),
    ("fresh-windows-metadata", "storage_lifecycle.py", 'visit(child, os.lstat(native(child)) if os.name == "nt" else entry.stat(follow_symlinks=False))', 'visit(child, entry.stat(follow_symlinks=False))', E+"test_fresh_metadata_ignores_stale_directory_entry_attributes"),
    ("clone-external-proof", "storage_lifecycle.py", 'return "external-commit-backup-required"', 'return None', L+"test_clean_clone_proof_rejects_missing_external_backup"),
    ("clone-unpublished-commit", "storage_lifecycle.py", 'if not git(backup, "for-each-ref", "--contains", commit, "--format=%(refname)", "refs/heads", "refs/tags", "refs/stash").strip():', 'if False:', L+"test_unpublished_clone_retained_when_backup_lacks_commit"),
    ("clone-dirty", "storage_lifecycle.py", 'if git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"):', 'if False:', L+"test_dirty_clone_retains_uncommitted_work"),
    ("clone-ignored-payload", "storage_lifecycle.py", 'if any(not set(Path(os.fsdecode(name)).parts) & REBUILDABLE for name in ignored.split(b"\\0") if name):', 'if False:', L+"test_ignored_necessary_bytes_without_restoration_copy_retain_clone"),
    ("preservation-cap", "storage_lifecycle.py", "if size + needed > cap:", "if False:", L+"test_preservation_capacity_keeps_existing_bytes_and_blocks_overflow"),
    ("duplicate-bytes", "storage_lifecycle.py", "return hashes(path) == hashes(target) and", "return True or", L+"test_duplicate_proof_compares_necessary_bytes_not_sizes_or_manifest"),
    ("sole-preservation-copy", "storage_lifecycle.py", "if not target or not any(absolute(target).is_relative_to(pin) for pin in permanent_pins) or not duplicate_verified(path, target, guard):", "if False:", L+"test_preserved_copy_removed_only_with_complete_duplicate"),
    ("config-observation", "storage_lifecycle.py", "if now - first <= 3 * DAY:", "if False:", L+"test_missing_project_requires_three_days_and_preserves_unrelated_toml"),
    ("config-changed-section", "storage_lifecycle.py", 'if item.get("section_sha256") == entry_hash else now', 'if True else now', L+"test_changed_project_section_restarts_missing_observation"),
    ("sessions-fourteen-days", "maintenance.py", "age <= 14 * DAY", "age <= 30 * DAY", E+"test_fourteen_day_sessions_archive_and_never_expire"),
    ("sessions-no-expiry", "maintenance.py", 'record("session-archive", p, "retained", reason="session-archives-have-no-expiry", bytes=p.stat().st_size)', 'p.unlink()', E+"test_fourteen_day_sessions_archive_and_never_expire"),
    ("mcp-disabled", "install_maintenance.py", 'changed, count = re.subn(pattern, "", text)', 'changed, count = text, 0', E+"test_deployment_disables_mcp_and_installs_preview_hook_and_rule_once"),
    ("task-end-rule", "install_maintenance.py", 'changed_agents = block.sub(TASK_RULE, agents_text) if block.search(agents_text) else agents_text.rstrip()+"\\n\\n"+TASK_RULE', 'changed_agents = agents_text', E+"test_deployment_disables_mcp_and_installs_preview_hook_and_rule_once"),
    ("hook-preview", "install_maintenance.py", '''str(runtime / "test_run.py") + '" --clean' ''', '''str(runtime / "test_run.py") + '" --clean --apply' ''', E+"test_deployment_disables_mcp_and_installs_preview_hook_and_rule_once"),
    ("heavy-single-suite", "workload.py", "if heavy:", "if False:", E+"test_heavy_execution_waits_for_the_existing_suite"),
    ("three-active-jobs", "workload.py", "for number in range(3):", "for number in range(4):", E+"test_three_general_jobs_block_the_fourth_and_release_on_exit"),
    ("approved-plan-hash", "maintenance.py", "if sha256(args.approved_plan) != args.plan_sha256:", "if False:", E+"test_apply_requires_reviewed_plan_and_matching_hash"),
    ("approved-candidates", "maintenance.py", "if approved is not None and identity(item) not in allowed:", "if False:", E+"test_changed_and_new_candidates_are_retained_after_preview"),
    ("approved-scope", "maintenance.py", 'if approved.get("home") != str(home) or approved.get("rules_hash") != rules_hash or approved.get("only") != args.only:', 'if False:', E+"test_local_rule_changes_require_another_preview"),
    ("failed-scratch-three-days", "test_run.py", "<= 3 * DAY:", "<= 7 * DAY:", T+"test_failed_scratch_expires_after_three_days"),
    ("backup-metadata-independent", "storage_lifecycle.py", 'snapshot(backup/".git", backup, guard)', 'pass  # removed independent metadata validation', L+"test_backup_objects_linked_into_candidate_are_not_independent"),
    ("test-configured-protection", "test_run.py", ', data.get("storage", {}).get("protected", []))', ')', T+"test_explicit_protected_scratch_keeps_bytes_before_payload_read"),
    ("protected-child-before-stat", "storage_lifecycle.py", 'if guard.forbidden(child):\n                    raise ValueError("protected-descendant")', 'if False:\n                    raise ValueError("protected-descendant")', L+"test_snapshot_rejects_protected_child_before_stat"),
    ("protected-recursive-discovery", "storage_lifecycle.py", '    if guard.forbidden(root) or guard.forbidden(root/".git"):\n        yield root; return\n    try:\n        guard.check(root, root.parent, allow_missing=True)\n    except (OSError, ValueError):\n        yield root; return\n', '', L+"test_recursive_discovery_rejects_protected_descendants_before_access"),
    ("skip-pinned-payload-walk", "storage_lifecycle.py", 'if guard.pinned(path) or value.get("pinned") or value.get("accepted") or value.get("state") == "running":', 'if False:', L+"test_known_retained_candidates_skip_payload_scans"),
    ("skip-recent-payload-walk", "storage_lifecycle.py", '                if root_age <= 3 * DAY:\n                    record(kind, path, "retained", reason="recent-within-three-days")\n                    continue\n                if root_age <= days * DAY:\n                    record(kind, path, "retained", reason="within-retention")\n                    continue\n', '', L+"test_known_retained_candidates_skip_payload_scans"),
    ("skip-unbacked-clone-walk", "storage_lifecycle.py", 'if str(path) not in backups:', 'if False:', L+"test_unbacked_clone_skips_payload_and_git_probes"),
    ("reuse-journal-snapshot", "maintenance.py", 'if action != "delete-expired" or "fingerprint" not in details:', 'if True:', E+"test_storage_journal_reuses_the_checked_candidate_snapshot"),
    ("repository-candidate-granularity", "storage_lifecycle.py", 'if not stat.S_ISDIR(info.st_mode):\n        return  # Clone discovery considers repositories, not individual task logs.', 'if not stat.S_ISDIR(info.st_mode):\n        yield root; return', L+"test_clone_discovery_skips_individual_task_logs"),
    ("clone-fourteen-day-retention", "storage_lifecycle.py", 'elif now - info.latest <= days * DAY:', 'elif True:', L+"test_clone_retention_boundary_is_fourteen_days"),
    ("preview-journal-sync-batching", "maintenance.py", 'if args.apply:\n                    stream.flush()\n                    os.fsync(stream.fileno())', 'if True:\n                    stream.flush()\n                    os.fsync(stream.fileno())', E+"test_preview_syncs_complete_journal_before_plan_without_per_item_flushes"),
    ("preview-journal-final-sync", "maintenance.py", '            stream.flush()\n            os.fsync(stream.fileno())\n        if not args.apply:', '        if not args.apply:', E+"test_preview_syncs_complete_journal_before_plan_without_per_item_flushes"),
    ("scheduler-home-pin", "maintenance.py", 'if not home.is_relative_to(cwd):', 'if True:', E+"test_scheduler_cwd_keeps_expired_storage_and_session_candidates"),
    ("complete-rollout-tail", "maintenance.py", 'window = min(limit, window * 2)', 'return latest', E+"test_recent_long_rollout_tail_stays_plain"),
    ("unknown-rollout-tail", "maintenance.py", 'return max(latest, time.time())', 'return latest', E+"test_oversized_rollout_tail_is_retained_conservatively"),
    ("config-backup-ancestry", "storage_lifecycle.py", 'backup = guard.check(home/"maintenance/config-backups"/(fingerprint+".toml"), home, allow_missing=True)', 'backup = home/"maintenance/config-backups"/(fingerprint+".toml")', L+"test_config_backup_reparse_keeps_config_and_external_bytes"),
    ("config-ledger-ancestry", "storage_lifecycle.py", 'ledger_path = guard.check(home/"maintenance/missing-projects.json", home, allow_missing=True)', 'ledger_path = home/"maintenance/missing-projects.json"', L+"test_missing_project_ledger_ancestry_rejected_before_write"),
    ("preservation-copy-persistent-pin", "storage_lifecycle.py", ' or not any(absolute(target).is_relative_to(pin) for pin in permanent_pins)', '', L+"test_cleanup_owned_duplicate_cannot_remove_both_restoration_copies"),
    ("preservation-pin-loaded-before-sweep", "storage_lifecycle.py", '    guard.pins.update(permanent_pins)\n', '', L+"test_configured_restoration_pin_survives_later_sweeps"),
    ("preservation-whole-copy-pin", "storage_lifecycle.py", 'absolute(target).is_relative_to(pin)', 'overlaps(absolute(target), pin)', L+"test_partial_restoration_pin_does_not_protect_a_sibling_payload"),
    ("clone-linked-worktree-owner", "storage_lifecycle.py", 'if os.path.lexists(registry):', 'if False:', L+"test_clone_owning_linked_worktree_preserves_detached_commit"),
    ("clone-shallow-backup", "storage_lifecycle.py", 'if git(backup, "rev-parse", "--is-shallow-repository").strip() != b"false":', 'if False:', L+"test_shallow_backup_does_not_authorize_clone_removal"),
    ("clone-partial-backup", "storage_lifecycle.py", 'if partial or any(packs.glob("*.promisor")):', 'if False:', L+"test_partial_or_promisor_backup_does_not_authorize_clone_removal"),
    ("clone-backup-object-closure", "storage_lifecycle.py", '    git(backup, "fsck", "--full", "--no-reflogs", "--no-dangling")\n', '', L+"test_backup_missing_tree_object_does_not_authorize_clone_removal"),
    ("storage-recent-session-pins", "maintenance.py", '                guard.pins.update(path for path in recent_cwds(home, now) if not home.is_relative_to(path))\n', '', L+"test_recent_session_checkout_is_pinned_during_storage_apply"),
    ("storage-session-home-container", "maintenance.py", 'if not home.is_relative_to(path)', 'if True', E+"test_recent_session_home_is_a_container_not_a_recursive_pin"),
    ("session-cwd-lexical-only", "maintenance.py", 'result.add(absolute(cwd))', 'result.add(Path(cwd).resolve())', E+"test_session_cwd_is_recorded_without_resolving_protected_path"),
    ("home-repo-guard", "task_run.py", 'if root == Path(os.path.abspath(account_home)):', 'if False:', E+"test_task_root_guard_rejects_home_repository_and_accepts_nested_project"),
    ("task-home-command-stop", "task_run.py", 'root = require_project_repository()', 'root = Path.cwd()', E+"test_task_root_guard_stops_before_command_or_scratch"),
    ("task-root-utf8", "task_run.py", 'encoding="utf-8"', 'encoding="ascii", errors="replace"', E+"test_task_root_guard_handles_unicode_home"),
    ("home-root-git-env", "task_run.py", "git_env = {key: value for key, value in os.environ.items() if not key.upper().startswith('GIT_')}", 'git_env = dict(os.environ)', E+"test_task_root_guard_ignores_inherited_git_routing"),
    ("task-handoff-rule", "install_maintenance.py", "- At task end update the project's handoff: progress, output paths, needed\n  worktrees and next step. Use its existing file, or CODEX_HOME/maintenance/handovers/<project>.md.\n", '', E+"test_deployed_rules_require_root_guard_and_project_handoff"),
    ("clone-index-flags", "storage_lifecycle.py", 'if index_flags_hide_changes(path, git):', 'if False:', L+"test_clone_index_flags_retain_hidden_tracked_edits"),
    ("worktree-index-flags", "maintenance.py", 'if index_flags_hide_changes(path, git):', 'if False:', "test_maintenance.MaintenanceTests.test_worktree_index_flags_retain_hidden_tracked_edits"),
]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--case",action="append")
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    selected=[case for case in CASES if not args.case or case[0] in args.case]
    results=[]
    for name,filename,before,after,test in selected:
        if os.name != "nt" and name in {"creation-time","readonly","fresh-windows-metadata"}:
            results.append({"case":name,"status":"platform-not-covered"});continue
        with tempfile.TemporaryDirectory(prefix="storage-mutation-") as tmp:
            fixture=Path(tmp)
            shutil.copytree(ROOT/"scripts",fixture/"scripts",ignore=shutil.ignore_patterns("__pycache__"))
            (fixture/"tests").mkdir()
            for filename_test in ("test_storage_lifecycle.py","test_storage_entrypoints.py","test_test_run.py","test_maintenance.py"):
                shutil.copy2(ROOT/"tests"/filename_test,fixture/"tests"/filename_test)
            source=fixture/"scripts"/filename
            original=source.read_text(encoding="utf-8")
            # Strip the optional whitespace in the hook recipe to retain a narrow match.
            if name == "hook-preview":before,after=before.rstrip(),after.rstrip()
            if before not in original:
                raise ValueError("mutation source no longer matches: "+name)
            command=[sys.executable,"-B","-m","unittest",test,"-v"]
            outputs=[]
            for phase in ("baseline","removed"):
                if phase == "removed":
                    changed=original.replace(before,after)
                    compile(changed,str(source),"exec")
                    source.write_text(changed,encoding="utf-8",newline="")
                run=subprocess.run(command,cwd=fixture/"tests",env={**os.environ,"PYTHONDONTWRITEBYTECODE":"1","CODEX_HOME":str(fixture/"unused-home")},capture_output=True,timeout=45)
                payload=run.stdout+run.stderr
                log=args.output/(name+"-"+phase+".log")
                with log.open("xb") as stream:
                    stream.write(payload);stream.flush();os.fsync(stream.fileno())
                outputs.append({"exit_code":run.returncode,"log":str(log),"sha256":hashlib.sha256(payload).hexdigest(),"assertion_failure":b"FAIL:" in payload,"error":b"ERROR:" in payload,"skipped":b"(skipped=" in payload})
            valid=outputs[0]["exit_code"]==0 and not outputs[0]["skipped"] and outputs[1]["exit_code"]!=0 and outputs[1]["assertion_failure"] and not outputs[1]["error"]
            results.append({"case":name,"test":test,"source":filename,"source_sha256":hashlib.sha256(original.encode()).hexdigest(),"removed":before,"replacement":after,"status":"killed" if valid else "unproven","runs":outputs})
            print(json.dumps({"case":name,"status":results[-1]["status"]}),flush=True)
            (args.output/"results.json").write_text(json.dumps(results,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"cases":len(results),"killed":sum(r["status"]=="killed" for r in results),"unproven":sum(r["status"]=="unproven" for r in results)}))
    return 1 if any(r["status"]=="unproven" for r in results) else 0


if __name__=="__main__":raise SystemExit(main())
