# Codex maintenance

`setup.ps1 -Apply` installs the maintenance runtime and two Windows Task Scheduler
tasks. For an existing installation, run these commands from the downloaded
harness checkout inside the active `CODEX_HOME`:

```powershell
.\scripts\install_maintenance.ps1                   # preview
.\scripts\install_maintenance.ps1 -Apply            # current user, daily 05:15
python .\scripts\maintenance.py                    # inspect candidates
```

The task runs at 05:15 in the Windows account's local time, after the separate
worktree cleanup owned by the other agent product. A missed schedule runs when
the user next signs in or the machine becomes available. The account must be
signed in; no password, SYSTEM identity, or elevated task is stored. Task names
are `CodexHarnessNightlyMaintenance` and `CodexHarnessPermissionRepair` (hourly).
Reinstallation updates only tasks pointing to this managed runner; a name
collision fails. Runtime/config drift is reported for inspection.

Installation deploys a **preview-only nightly job** and preview-only Stop /
SessionEnd scratch sweeps. Permission repair retains its separate ACL-only role.
Deletion requires an explicit reviewed plan; installation does not approve one.

## 1. Linked worktrees

The nightly task searches only `CODEX_HOME/worktrees`, up to three container
levels. Its linked-worktree pass reports clones; the storage pass below evaluates
explicitly configured clones separately. A candidate must be registered, unlocked,
completely readable, clean including all untracked files, and idle for more than
three days. The newest file/directory and Git metadata modification times both
count as activity. Recent rollout `cwd` values, the running checkout, and
`maintenance/local.json`'s optional `pinned_worktrees` list protect active work.

A branch or tag must retain HEAD (a descendant tip is sufficient). Otherwise,
the tool first creates and verifies `refs/heads/maintenance-retained/<HEAD>` in
the owning repository. It copies necessary ignored payloads into
`maintenance/preserved-worktrees/<name>-<unique-id>/files`, verifies SHA-256, writes a
preservation manifest, checks the candidate again, then calls Git's worktree
removal. `__pycache__`, `.pytest_cache`, `.mypy_cache` and `.ruff_cache` are
rebuildable and omitted. A patch, file manifest and required payload bytes make
recovery possible; a filename list alone cannot restore untracked data. The
2 GiB preservation cap blocks new removals when a backup would exceed it.
After 30 days a complete, independently verified duplicate permits expiry;
sole copies remain retained even when the cap is already exceeded.
The manifest records `data_directory: files`, keeping user filenames separate
from its `preservation.json` receipt. Earlier receipts without `data_directory`
refer to files directly below their preservation directory and remain valid.

Git calls have a 30-second deadline. Unreadable data, nested repositories,
submodules, reparse points, locks, Git failures, and concurrent changes block
that candidate. Each decision is written to a flushed journal. Dirty worktrees
remain listed for the user. Existing linked worktrees in `Documents/Codex`
retain their separate cleanup owner. Its independent clones require an explicit
scope, a clean tree, 14 idle days and externally reachable retained commits.

## 2. Rollouts and temporary files

Both `sessions` and `archived_sessions` use the later of file modification time
and the most recent readable JSONL event timestamp. This protects a reopened
rollout even when Windows has deferred its LastWriteTime update:

| Age | Action |
| --- | --- |
| Up to 14 days | Keep plain JSONL |
| More than 14 days | Verified gzip into `maintenance/session-archives/<area>/<relative-name>.gz` |
| Existing archives, any age | Keep; session data has no expiry |

The newest rollout in **each** source area always stays as plain JSONL,
including its `rate_limits` records, even when that newest rollout is old.
Compression streams bytes, verifies the decompressed digest and unchanged
source, publishes the archive and receipt, then removes the source. Archives
retain original age. A collision keeps the source for inspection. Other files
in the session directories and runtime databases remain untouched.

Preexisting `rollout-*.jsonl.zst` and `.jsonl.gz` files already satisfy the
compression step. They remain retained at every age.
They stay in their existing format; handling them adds no zstd dependency.

Compressed conversations require restoring their original JSONL path before
the app can read them again; the app's database is not rewritten. To restore a
gzip, decompress its complete bytes to the original relative path in the source
area, compare the receipt's `source_sha256`, and retain the gzip until verified.
Stop concurrent maintenance while doing a manual restore. A latest rollout is
never moved, so quota readers can continue reading the usual newest JSONL.

In `CODEX_HOME/tmp`, a top-level entry expires after seven days only if every
descendant is readable and older than seven days. A recent child protects its
whole entry. Junctions/symlinks and unreadable trees are reported. A kernel lock
serializes maintenance runs and releases automatically after process exit.

## 3. Sandbox file permissions

The hourly task repairs generated test directories under Codex worktrees/tmp.
Stop and SessionEnd hooks inspect only `.ai`, `.pytest_cache`, `temp`, and `tmp`
under the current task in Codex-owned scope. Repair adds inheritable FullControl
for the current user, retains other ACL entries, and restores a sandbox-owned
directory's owner to that user. Original SDDL is saved under
`maintenance/acl-backups` before each change. Failure is visible in the task log;
it never turns an unreadable worktree into a deletion candidate.

For a specifically authorized existing ownership problem, use:

```powershell
.\scripts\repair_maintenance_acl.ps1 -Path $AffectedCodexTask
.\scripts\repair_maintenance_acl.ps1 -Apply -Path $AffectedCodexTask
```

Targets are restricted to descendants of the active Codex home or the user's
`Documents/Codex`. The explicit-path mode changes ACLs and sandbox ownership;
it performs no cleanup. It adds no `safe.directory` exemptions and changes no
sandbox, approval, or network setting. Protected ACLs or unavailable Windows
rights remain an explicit failure requiring a narrowly scoped repair.

## 4. MCP backend lifetime and Git hangs

Installation backs up config and removes the `codebase-memory-mcp` server table
and its subtables. Other MCP entries stay intact. An already absent server stays
absent; the installer starts no backend. Reconnection is needed for existing
clients to adopt a changed config. A future explicit reintroduction must enforce
one backend per Codex process and cleanup at conversation end. The bundled
process containment utility remains available to the test runner.

## Evidence and recovery

### Test scratch at the point of execution

Use the project's Python interpreter to invoke the installed runner:

```powershell
python "$env:USERPROFILE/.codex/maintenance/runtime/test_run.py" --timeout 600 --pytest -- -q tests
python "$env:USERPROFILE/.codex/maintenance/runtime/test_run.py" --timeout 600 -- python -m unittest discover -s tests -v
```

Each invocation owns a unique directory under `tmp/test-runs`. TEMP,
TMP, TMPDIR, coverage, mypy and ruff caches point into that run. Pytest mode
supplies a unique basetemp and cache_dir; `python -m pytest` commands are also
recognized. Python bytecode writes are disabled in child processes. Test scripts
can use `CODEX_TEST_SCRATCH` for their own disposable outputs. Source files and
deliverables should keep their normal paths.

A successful run removes its scratch immediately. Failed/timeout scratch stays
for three days; complete stdout/stderr and its receipt stay in `test-logs` for
30 days. Stop/SessionEnd hooks and the nightly task sweep abandoned/expired runs,
skip fresh payloads, respect a live kernel
lease and retains unreadable or redirected trees. Windows creates the test
process suspended and contains its descendants in a kill-on-close Job Object
before execution. Timeout, normal completion and runner death reap that job.
POSIX uses process groups; a surviving recorded group protects an abandoned run.

The wrapper controls scratch used through these variables and pytest options.
Tests that explicitly write another path still need their own scoped cleanup.
Existing sessions must adopt this entry point; installing it cannot redirect an
already-running test. Unrecognized old repo files remain outside its ownership.

Use `task_run.py --timeout SECONDS -- COMMAND ...` for other autonomous commands.
Three shared kernel leases limit executing managed jobs. Test runs and maintenance
share one heavy I/O lease; waiting heavy jobs do not consume general slots. Locks
release on exit/crash. Desktop chat count and commands outside these entrypoints
are outside this process limit. New clones and worktrees belong under
`CODEX_HOME/worktrees`; new scratch belongs under `CODEX_HOME/tmp`.

## Storage plans and retention

`maintenance/local.json` contains host-local `storage` settings: `legacy_roots`,
`legacy_paths`, `clone_roots`, exact `owned_paths`, `clone_backups`, `pinned`,
and optional additional `protected` paths. Shared TEMP names alone establish no
ownership. Missing/unreadable/redirected candidates are retained and journaled.
All policies retain activity within three days (including creation and metadata-change time), pins,
accepted outcomes and unbacked user edits. Ancestor pin files are `.codex-pin`,
`.maintenance-pin` or `.lifecycle-pin`.

Outputs in visualizations/generated_images/harness-runs and harness artifacts /
evidence expire after 30 days. Git-tracked outcomes and checkpoint references
protect accepted results. Unreadable/oversized checkpoint reference indexes retain
potentially referenced harness outputs. Completed, unreferenced checkpoints expire
after 30 days; unknown state requires a completion receipt. Reports/logs and
installation/config/schedule backups use 30 days; ACL backups use 90 days. The
newest rollback installation/ACL backup remains retained. Shared legacy scratch
uses seven days and requires exact ownership. Clone removal requires clean status,
14 idle days and every HEAD/local branch/tag/stash retained by reachable refs in
the configured independent `clone_backups` repository. Shared object stores,
submodules and necessary ignored data retain the clone for inspection.

Project TOML sections expire only after a missing-path observation lasting more
than three days. Other TOML bytes are preserved, config is backed up, and concurrent
changes stop the write. SQLite databases require a separate offline, verified
backup procedure. Home-repository Git history and model installations stay outside
retention. Protected product homes, backup paths and reparse points are rejected
before traversal.

```powershell
python scripts/maintenance.py --plan "$env:CODEX_HOME/maintenance/review.json"
# After reviewing that exact plan and receiving user approval:
python scripts/maintenance.py --apply --approved-plan "$env:CODEX_HOME/maintenance/review.json" --plan-sha256 REVIEWED_SHA256
```

Plan identity covers runtime/local rules and each candidate fingerprint. Apply
rechecks status, pins, activity and ownership; new/changed candidates require a
new preview. Each intent, retained decision, removal or block goes into a flushed
journal. Compression input bytes are reported separately from deletion bytes;
actual compression savings and physical disk savings are unknown before apply.

`maintenance/reports` holds decision journals; `maintenance/logs` holds complete
scheduled stdout/stderr and failed targets. `install-backups` retains the prior
config, hooks and runtime ownership manifest. Review these local records before
repeating a failed mutation. Logs, sessions, preserved files, host paths, and
machine identities belong only in the local Codex home, never in this repository.

To pause scheduling, disable the two named tasks. Restore backed-up config only
after reviewing its scope, and remove owned hook entries to undo integration. Runtime
ownership hashes protect local edits during later installation. The portable
Python retention CLI can be invoked manually on other platforms; Windows setup
is the scheduling entry point supplied by this change.
