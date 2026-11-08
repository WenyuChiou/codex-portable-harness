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

## 1. Linked worktrees

The nightly task searches only `CODEX_HOME/worktrees`, up to three container
levels. Clones are report-only. A candidate must be registered, unlocked,
completely readable, clean including all untracked files, and idle for more than
three days. The newest file/directory and Git metadata modification times both
count as activity. Recent rollout `cwd` values, the running checkout, and
`maintenance/local.json`'s optional `pinned_worktrees` list protect active work.

A branch or tag must retain HEAD (a descendant tip is sufficient). Otherwise,
the tool first creates and verifies `refs/heads/maintenance-retained/<HEAD>` in
the owning repository. It copies **all** ignored files into
`maintenance/preserved-worktrees/<name>-<unique-id>/files`, verifies SHA-256, writes a
preservation manifest, checks the candidate again, then calls Git's worktree
removal. Preserved files have no automatic expiry; this conservative default
also preserves rebuildable ignored caches. Recover those files from the manifest
before removing their preserved copies manually.
The manifest records `data_directory: files`, keeping user filenames separate
from its `preservation.json` receipt. Earlier receipts without `data_directory`
refer to files directly below their preservation directory and remain valid.

Git calls have a 30-second deadline. Unreadable data, nested repositories,
submodules, reparse points, locks, Git failures, and concurrent changes block
that candidate. Each decision is written to a flushed journal. Dirty worktrees
remain listed for the user. The cleanup never scans `Documents/Codex` for
deletion; its linked worktrees already have their own cleanup owner.

## 2. Rollouts and temporary files

Both `sessions` and `archived_sessions` use the later of file modification time
and the most recent readable JSONL event timestamp. This protects a reopened
rollout even when Windows has deferred its LastWriteTime update:

| Age | Action |
| --- | --- |
| Up to 30 days | Keep plain JSONL |
| More than 30, up to 90 days | gzip into `maintenance/session-archives/<area>/<relative-name>.gz` |
| More than 90 days | Delete expired rollout or owned, unmodified gzip |

The newest rollout in **each** source area always stays as plain JSONL,
including its `rate_limits` records, even when that newest rollout is old.
Compression streams bytes, verifies the decompressed digest and unchanged
source, publishes the archive and receipt, then removes the source. Archives
retain original age. A collision keeps the source for inspection. Other files
in the session directories and runtime databases remain untouched.

Preexisting `rollout-*.jsonl.zst` and `.jsonl.gz` files already satisfy the
compression step. They expire after 90 days using their preserved modification
time, while the newest preexisting compressed file in each area is retained.
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

For an existing `mcp_servers.codebase-memory-mcp` entry with a plain command and
no custom argv, installation backs up `config.toml`, then registers a lightweight
stdio facade. Each request starts a native backend, performs its handshake,
forwards the request/result, and reaps the backend immediately. The facade stays
connected to the client and starts another backend for the next request.
Initialization alone is replayed. Tool calls are never retried automatically.

Deadlines are 30 seconds for startup, 60 seconds for ordinary requests, and 900
seconds for `index_repository`. Codex's outer tool wait is 960 seconds. A Windows
Job Object contains the backend before it starts executing, so response, client
EOF, timeout, or wrapper death also reaps Git descendants. POSIX uses a process
group. A timeout is returned as an MCP error; the next request remains usable.
This adapter is intended for the server's stateless tools, not stateful resource
subscriptions. It adds per-request startup cost and a lightweight facade per
client connection, while eliminating idle native backends. Existing MCP
connections need a reload/reconnect to pick up the new command.

The bundled timeout is process-tree containment, including Git, rather than a
new native setting in codebase-memory-mcp. Custom MCP args and unknown config
syntax stop automatic migration. Other MCP servers and other products' files
are outside this installer. Full native server updates remain a separate task.

## Evidence and recovery

### Test scratch at the point of execution

Use the project's Python interpreter to invoke the installed runner:

```powershell
python "$env:USERPROFILE/.codex/maintenance/runtime/test_run.py" --timeout 600 --pytest -- -q tests
python "$env:USERPROFILE/.codex/maintenance/runtime/test_run.py" --timeout 600 -- python -m unittest discover -s tests -v
```

Each invocation owns a unique directory under `maintenance/test-runs`. TEMP,
TMP, TMPDIR, coverage, mypy and ruff caches point into that run. Pytest mode
supplies a unique basetemp and cache_dir; `python -m pytest` commands are also
recognized. Python bytecode writes are disabled in child processes. Test scripts
can use `CODEX_TEST_SCRATCH` for their own disposable outputs. Source files and
deliverables should keep their normal paths.

A successful run removes its scratch immediately. Failed/timeout scratch stays
for seven days; complete stdout/stderr and its receipt stay in `test-logs` for
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

`maintenance/reports` holds decision journals; `maintenance/logs` holds complete
scheduled stdout/stderr and failed targets. `install-backups` retains the prior
config, hooks and runtime ownership manifest. Review these local records before
repeating a failed mutation. Logs, sessions, preserved files, host paths, and
machine identities belong only in the local Codex home, never in this repository.

To pause scheduling, disable the two named tasks. Restore the backed-up MCP entry
and remove only the two maintenance hook entries to undo integration. Runtime
ownership hashes protect local edits during later installation. The portable
Python retention CLI can be invoked manually on other platforms; Windows setup
is the scheduling entry point supplied by this change.
