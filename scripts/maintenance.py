"""Conservative Codex-only retention. Preview by default; every decision is journaled."""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
import uuid

from common import active_codex_home, atomic_write_json, sha256
from storage_lifecycle import Guard, snapshot, REBUILDABLE, preserve_capacity

DAY = 86400
OWNER = "codex-portable-harness-maintenance-v1"


def safe(path: Path, root: Path) -> Path:
    """Reject junctions, symlinks, lexical escapes and the root itself."""
    return Guard(root).check(path, root, allow_missing=True)


def tree(path: Path, root: Path) -> list[Path]:
    """An unreadable child invalidates the entire candidate, never a partial walk."""
    safe(path, root)
    result = []
    def visit(item):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"reparse point: {item}")
        result.append(item)
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(item) as entries:
                for entry in entries:
                    visit(Path(entry.path))
    visit(path)
    return result


def signature(path: Path, root: Path) -> tuple[str, float, int]:
    records, latest, size = [], 0.0, 0
    for p in tree(path, root):
        s = p.stat()
        records.append((p.relative_to(path).as_posix(), s.st_size, s.st_mtime_ns, s.st_mode))
        latest = max(latest, s.st_mtime)
        if p.is_file():
            size += s.st_size
    payload = json.dumps(records, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest(), latest, size


def run(args: list[str], timeout: float = 30, *, env: dict[str, str] | None = None) -> bytes:
    """No shell; a hung Git probe never becomes permission to delete."""
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                         start_new_session=os.name != "nt")
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        else:
            import signal
            os.killpg(p.pid, signal.SIGKILL)
        p.kill()
        p.communicate()
        raise TimeoutError(f"command timed out after {timeout}s: {args[:4]}")
    if p.returncode:
        raise RuntimeError(f"exit {p.returncode}: {args[:4]}: {err.decode(errors='replace')[-2000:]}")
    return out


def git(path: Path, *args: str) -> bytes:
    # Routing, config injection and trace destinations must come from this
    # inspected repository, not from inherited GIT_* overrides.
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.longpaths=true", "-C", str(path), *args], env=env)


def recent_cwds(home: Path, now: float) -> set[Path]:
    result = set()
    for area in ("sessions", "archived_sessions"):
        base = home / area
        if not base.exists():
            continue
        for p in tree(base, home):
            if p.is_file() and p.suffix == ".jsonl" and now - rollout_activity(p) < 3 * DAY:
                with p.open("rb") as f:
                    raw = f.readline(1024 * 1024)
                value = json.loads(raw)
                cwd = value.get("payload", {}).get("cwd")
                if cwd:
                    result.add(Path(cwd).resolve())
    return result


def worktree_candidates(root: Path):
    if not root.exists():
        return
    pending = [(root, 0)]
    while pending:
        path, depth = pending.pop()
        if path != root:
            safe(path, root)
        if (path / ".git").exists():
            yield path
            continue
        if depth < 3:
            with os.scandir(path) as entries:
                pending.extend((Path(e.path), depth + 1) for e in entries if e.is_dir(follow_symlinks=False))


def clean_status(path: Path) -> bytes:
    return git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none")


def allowed_metadata(path: Path) -> Path:
    def check(candidate):
        if Guard(path.parent).forbidden(candidate):
            raise ValueError("owning Git metadata belongs to another agent product; candidate retained")
    check(path)  # Lexical rejection must happen before filesystem resolution.
    resolved = path.resolve()
    check(resolved)
    return resolved


def metadata_hint(path: Path) -> tuple[Path, Path]:
    # Inspect the pointer in Codex scope before launching Git, which would read
    # the owning repository's configuration. Check commondir before following it.
    pointer = safe(path / ".git", path).read_text(encoding="utf-8").strip()
    if not pointer.startswith("gitdir: "):
        raise ValueError("unsupported worktree Git pointer")
    metadata = allowed_metadata(path / pointer[len("gitdir: "):])
    common_file = allowed_metadata(metadata / "commondir")
    common = allowed_metadata(metadata / common_file.read_text(encoding="utf-8").strip())
    return common, metadata


def clean_worktrees(home: Path, apply: bool, now: float, record, pinned: set[Path]):
    root = home / "worktrees"
    active = recent_cwds(home, now)
    for path in worktree_candidates(root):
        try:
            safe(path, root)
            if (path / ".git").is_dir():
                record("worktree", path, "clone-report-only")
                continue
            if any(p == path.resolve() or p.is_relative_to(path.resolve()) for p in pinned | active):
                record("worktree", path, "active-or-pinned")
                continue
            hinted_common, hinted_metadata = metadata_hint(path)
            common = Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip())
            metadata = Path(git(path, "rev-parse", "--absolute-git-dir").decode().strip())
            if allowed_metadata(common) != hinted_common or allowed_metadata(metadata) != hinted_metadata:
                raise ValueError("Git metadata pointer changed during inspection")
            # Only a registered linked worktree, never a clone/submodule/arbitrary .git pointer.
            if metadata.parent != common / "worktrees":
                raise ValueError("unexpected linked worktree metadata")
            registered = git(path, "worktree", "list", "--porcelain", "-z")
            entry = b"worktree " + str(path).replace("\\", "/").encode() + b"\0"
            if entry not in registered or (metadata / "locked").exists():
                raise ValueError("unregistered or locked worktree")
            if clean_status(path):
                record("worktree", path, "dirty-report-only")
                continue
            before, latest, size = signature(path, root)
            meta_latest = max(p.stat().st_mtime for p in tree(metadata, common))
            if now - max(latest, meta_latest) <= 3 * DAY:
                record("worktree", path, "recent")
                continue
            if (path / ".gitmodules").exists():
                raise ValueError("submodules require manual preservation")
            head = git(path, "rev-parse", "HEAD").decode().strip()
            refs = git(path, "for-each-ref", "--contains", head, "--format=%(refname)", "refs/heads", "refs/tags")
            ref = "refs/heads/maintenance-retained/" + head
            ignored = [os.fsdecode(x) for x in git(path, "ls-files", "--others", "--ignored", "--exclude-standard", "-z").split(b"\0") if x]
            skipped = [name for name in ignored if {part.casefold() for part in Path(name).parts} & REBUILDABLE]
            ignored = [name for name in ignored if name not in skipped]
            needed = sum(safe(path/name, path).stat().st_size for name in ignored)
            if needed:
                metadata_allowance = 16384 + sum(len(json.dumps(name).encode()) + 512 for name in ignored + skipped)
                preserve_capacity(home, needed + metadata_allowance)
            record("worktree", path, "eligible", head=head, bytes=size, ignored_files=len(ignored), preserve_ref=None if refs else ref)
            if not apply:
                continue
            if signature(path, root)[0] != before or clean_status(path) or git(path, "rev-parse", "HEAD").decode().strip() != head:
                raise ValueError("candidate changed during inspection")
            if not refs:
                existing = git(path, "for-each-ref", ref, "--format=%(objectname)").strip()
                if existing and existing.decode() != head:
                    raise ValueError("preservation ref collision")
                if not existing:
                    git(path, "update-ref", ref, head, "0" * len(head))
                if git(path, "rev-parse", ref).decode().strip() != head:
                    raise ValueError("preservation ref verification failed")
            backup = home / "maintenance" / "preserved-worktrees" / (path.name + "-" + uuid.uuid4().hex)
            safe(backup, home)
            saved = []
            for name in ignored:
                source = safe(path / name, path)
                if not source.is_file():
                    raise ValueError(f"unsupported ignored object: {name}")
                # User filenames live in a separate namespace from receipts.
                target = safe(backup / "files" / name, home)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = sha256(source)
                shutil.copy2(source, target)
                if sha256(target) != digest or sha256(source) != digest:
                    raise ValueError(f"ignored content changed: {name}")
                saved.append({"path": name, "sha256": digest})
            if saved:
                (backup/"changes.patch").write_bytes(git(path, "diff", "--binary", "HEAD"))
                atomic_write_json(backup / "preservation.json", {"owner": OWNER, "created": now, "source": str(path), "head": head, "data_directory": "files", "files": saved, "untracked": [], "rebuildable_skipped": skipped})
            if signature(path, root)[0] != before or clean_status(path):
                raise ValueError("candidate changed before removal; preserved copies retained")
            # Git performs its own final clean/untracked check. An intervening
            # user edit must keep the worktree even after our snapshot passed.
            safe(path, root)
            # Windows cannot delete a process's current directory. Run from
            # the owning metadata directory, which is outside the candidate.
            git(common, "worktree", "remove", str(path))
            if path.exists():
                raise ValueError("Git did not remove the worktree")
            record("worktree", path, "removed", head=head, preserved=str(backup) if saved else None)
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            record("worktree", path, "blocked", error=str(exc))


def stable_stat(path: Path):
    s = path.stat()
    return s.st_size, s.st_mtime_ns, s.st_ino


ROLLOUT_TAIL_LIMIT = 8 * 1024 * 1024


def rollout_activity(path: Path) -> float:
    # Windows may delay LastWriteTime until an app closes its append handle.
    # Recent event timestamps therefore also protect a reopened older rollout.
    latest = path.stat().st_mtime
    with path.open("rb") as f:
        size = f.seek(0, os.SEEK_END)
        limit = min(size, ROLLOUT_TAIL_LIMIT)
        window = min(limit, 64 * 1024)
        while True:
            offset = size - window
            f.seek(offset)
            tail = f.read(window)
            # A large final event can occupy the entire initial window. Grow
            # backward until its start is present, rather than discarding it.
            if offset:
                boundary = tail.find(b"\n")
                tail = tail[boundary + 1:] if boundary >= 0 else b""
            for line in reversed(tail.splitlines()):
                try:
                    value = json.loads(line)
                    timestamp = value.get("timestamp")
                    if timestamp:
                        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                        if parsed.tzinfo is not None:
                            return max(latest, parsed.timestamp())
                except (ValueError, TypeError, AttributeError):
                    continue
            if not offset:
                return latest
            if window >= limit:
                # Unknown activity at the bounded read limit is retained.
                return max(latest, time.time())
            window = min(limit, window * 2)


def archive_one(source: Path, target: Path, root: Path):
    safe(source, root)
    safe(target, root)
    if target.exists():
        raise ValueError("archive collision; source retained")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".partial-" + uuid.uuid4().hex)
    before = stable_stat(source)
    digest = hashlib.sha256()
    try:
        with source.open("rb") as f, temporary.open("xb") as dest:
            with gzip.GzipFile(fileobj=dest, mode="wb", mtime=0, compresslevel=1) as z:
                for block in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(block)
                    z.write(block)
            dest.flush()
            os.fsync(dest.fileno())
        check = hashlib.sha256()
        with gzip.open(temporary, "rb") as z:
            for block in iter(lambda: z.read(1024 * 1024), b""):
                check.update(block)
        if digest.digest() != check.digest() or stable_stat(source) != before or sha256(source) != digest.hexdigest():
            raise ValueError("source changed or archive verification failed")
        # Keep age from original activity; compression never renews retention.
        os.utime(temporary, ns=(source.stat().st_atime_ns, int(rollout_activity(source) * 1e9)))
        os.replace(temporary, target)
        atomic_write_json(target.with_name(target.name + ".receipt.json"),
                          {"owner": OWNER, "source_sha256": digest.hexdigest(), "archive_sha256": sha256(target), "mtime_ns": before[1]})
        if stable_stat(source) != before:
            raise ValueError("source changed before unlink; archive retained")
        source.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()


def retain_sessions(home: Path, apply: bool, now: float, record, pinned=()):
    cold = home / "maintenance" / "session-archives"
    guard = Guard(home, pinned)
    for area in ("sessions", "archived_sessions"):
        base = home / area
        if not base.exists():
            continue
        inventory = [p for p in tree(base, home) if p.is_file() and p.name.startswith("rollout-")]
        files = [p for p in inventory if p.suffix == ".jsonl"]
        # Keep newest in EACH area as plain JSONL, including rate_limits records.
        activity = {p: rollout_activity(p) for p in files}
        newest = max(files, key=activity.get) if files else None
        for p in files:
            try:
                age = now - activity[p]
                if p == newest or age <= 14 * DAY or guard.pinned(p):
                    record("session", p, "retained", reason="newest-rate-limits-or-recent-or-pinned", bytes=p.stat().st_size)
                    continue
                safe(p, home)
                record("session", p, "compress", bytes=p.stat().st_size)
                if apply:
                    archive_one(p, cold / area / p.relative_to(base).with_suffix(".jsonl.gz"), home)
            except (OSError, ValueError) as exc:
                record("session", p, "blocked", error=str(exc))
        # Existing native/compressed archives already satisfy compression. Apply
        # expiry using preserved filesystem activity, without a new dependency.
        packed = [p for p in inventory if p.name.endswith((".jsonl.zst", ".jsonl.gz"))]
        for p in packed:
            record("session-native-archive", p, "retained", reason="session-archives-have-no-expiry", bytes=p.stat().st_size)
    if cold.exists():
        for p in tree(cold, home):
            if p.is_file() and p.suffix == ".gz":
                record("session-archive", p, "retained", reason="session-archives-have-no-expiry", bytes=p.stat().st_size)


def clean_tmp(home: Path, apply: bool, now: float, record):
    root = home / "tmp"
    if not root.exists():
        return
    safe(root, home)
    for path in root.iterdir():
        try:
            before, latest, size = signature(path, root)
            if now - latest <= 7 * DAY:
                continue
            record("tmp", path, "delete-expired", bytes=size)
            if apply and signature(path, root)[0] == before:
                safe(path, root)
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        except (OSError, ValueError) as exc:
            record("tmp", path, "blocked", error=str(exc))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--only", choices=["all", "worktrees", "retention", "storage"], default="all")
    parser.add_argument("--plan", type=Path, help="preview plan output inside CODEX_HOME")
    parser.add_argument("--approved-plan", type=Path)
    parser.add_argument("--plan-sha256")
    args = parser.parse_args()
    home = active_codex_home()
    config = home / "maintenance" / "local.json"
    value = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    source_parent = Path(__file__).resolve().parent
    pinned = {source_parent if source_parent.name == "runtime" else source_parent.parent}
    cwd = Path.cwd()
    if not home.is_relative_to(cwd):
        pinned.add(cwd)  # Keep active checkouts; scheduler home/ancestors are containers.
    pinned.update(Path(p) for p in value.get("pinned_worktrees", []))
    pinned.update(Path(p) for p in value.get("storage", {}).get("pinned", []))
    guard = Guard(home, pinned, value.get("storage", {}).get("protected", []))
    rules_hash = hashlib.sha256(b"".join(Path(__file__).with_name(name).read_bytes() for name in ("maintenance.py", "storage_lifecycle.py", "workload.py")) + json.dumps(value, sort_keys=True).encode()).hexdigest()
    approved = None
    if args.apply:
        if not args.approved_plan or not args.plan_sha256:
            parser.error("--apply requires --approved-plan and its reviewed --plan-sha256")
        safe(args.approved_plan, home)
        if sha256(args.approved_plan) != args.plan_sha256:
            parser.error("approved plan hash mismatch")
        approved = json.loads(args.approved_plan.read_text(encoding="utf-8"))
        if approved.get("home") != str(home) or approved.get("rules_hash") != rules_hash or approved.get("only") != args.only:
            parser.error("approved plan scope or runtime changed; preview again")
    state = safe(home / "maintenance", home)
    state.mkdir(exist_ok=True)
    lock = state / "run.lock"
    # Kernel lock auto-releases after a crash; the file itself is persistent.
    with lock.open("a+b") as run_guard:
        try:
            if os.name == "nt":
                import msvcrt
                run_guard.seek(0)
                if not run_guard.read(1):
                    run_guard.write(b"0")
                    run_guard.flush()
                run_guard.seek(0)
                msvcrt.locking(run_guard.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(run_guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("Another maintenance run holds the lock")
            return 2
        stamp = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        report = safe(state / "reports" / (stamp + ".jsonl"), home)
        report.parent.mkdir(exist_ok=True)
        plan = safe(args.plan or state/"plans"/(stamp+".json"), home)
        guard.pins.update({report, plan})
        if args.approved_plan:
            guard.pins.add(args.approved_plan)
        failures = []
        mutations = []
        def identity(item):
            return (item["kind"], item["path"], item["action"], item["fingerprint"], json.dumps(item.get("entries"), sort_keys=True))
        allowed = {identity(item) for item in approved["mutations"]} if approved else set()
        now = time.time()
        from workload import work_slot
        with work_slot(home, heavy=True), report.open("x", encoding="utf-8") as stream:
            def record(kind, path, action, **details):
                if action in {"eligible", "delete-expired", "compress", "prune-projects"}:
                    # Storage already supplies its complete checked snapshot.
                    # Apply's remove() resnapshots immediately before mutation.
                    if action != "delete-expired" or "fingerprint" not in details:
                        info = snapshot(path, home if Path(path).is_relative_to(home) else Path(path).parent, guard)
                        if info.pinned or now - info.latest <= 3 * DAY:
                            raise ValueError("pinned-or-recent-candidate-retained")
                        details.setdefault("fingerprint", info.fingerprint)
                    item = {"kind": kind, "path": str(path), "action": action, **details}
                    if approved is not None and identity(item) not in allowed:
                        raise ValueError("candidate-not-in-approved-preview")
                    mutations.append(item)
                value = {"kind": kind, "path": str(path), "action": action, "apply": args.apply, **details}
                stream.write(json.dumps(value, ensure_ascii=False) + "\n")
                if args.apply:
                    stream.flush()
                    os.fsync(stream.fileno())
                print(json.dumps(value, ensure_ascii=False), flush=True)
                if action == "blocked":
                    failures.append(value)
            import storage_lifecycle
            for name, action in [("worktrees", lambda: clean_worktrees(home, args.apply, now, record, pinned)),
                                 ("retention", lambda: retain_sessions(home, args.apply, now, record, pinned)),
                                 ("storage", lambda: (storage_lifecycle.sweep(home, args.apply, now, record, guard, value, git), storage_lifecycle.prune_projects(home, args.apply, now, record, guard)))]:
                if args.only in (name, "all"):
                    try:
                        action()
                    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                        record(name, home, "blocked", error=str(exc))
            # Preview changes no payload. Flush its complete journal once before
            # publishing a plan; apply intents/outcomes remain synchronous above.
            stream.flush()
            os.fsync(stream.fileno())
        if not args.apply:
            if plan.exists():
                raise ValueError("plan output already exists")
            atomic_write_json(plan, {"home": str(home), "only": args.only, "rules_hash": rules_hash, "created": now, "mutations": mutations})
        print(json.dumps({"report": str(report), "sha256": sha256(report), "blocked": len(failures), "plan": str(plan) if not args.apply else None, "plan_sha256": sha256(plan) if not args.apply else None}))
        return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
