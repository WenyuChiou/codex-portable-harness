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

DAY = 86400
OWNER = "codex-portable-harness-maintenance-v1"


def safe(path: Path, root: Path) -> Path:
    """Reject junctions, symlinks, lexical escapes and the root itself."""
    path = Path(os.path.abspath(path))
    root = Path(os.path.abspath(root))
    if path == root or not path.is_relative_to(root):
        raise ValueError(f"outside maintenance scope: {path}")
    for p in (path, *path.parents):
        if p.exists() or p.is_symlink():
            info = p.lstat()
            if p.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError(f"reparse point: {p}")
        if p == root:
            break
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"resolved path escapes scope: {path}")
    return path


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
    return run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(path), *args], env=env)


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
        if any(part.lower() in {".claude", ".gemini", ".opencode"} for part in candidate.parts):
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
                target = safe(backup / name, home)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = sha256(source)
                shutil.copy2(source, target)
                if sha256(target) != digest or sha256(source) != digest:
                    raise ValueError(f"ignored content changed: {name}")
                saved.append({"path": name, "sha256": digest})
            if saved:
                atomic_write_json(backup / "preservation.json", {"source": str(path), "head": head, "files": saved})
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


def rollout_activity(path: Path) -> float:
    # Windows may delay LastWriteTime until an app closes its append handle.
    # Recent event timestamps therefore also protect a reopened older rollout.
    latest = path.stat().st_mtime
    with path.open("rb") as f:
        size = path.stat().st_size
        offset = max(0, size - 1024 * 1024)
        f.seek(offset)
        lines = f.read().splitlines()
    if offset and lines:
        lines = lines[1:]  # First line can be truncated by the tail boundary.
    for line in reversed(lines):
        try:
            value = json.loads(line)
            timestamp = value.get("timestamp")
            if timestamp:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    return max(latest, parsed.timestamp())
        except (ValueError, TypeError, AttributeError):
            continue
    return latest


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


def retain_sessions(home: Path, apply: bool, now: float, record):
    cold = home / "maintenance" / "session-archives"
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
                if p == newest or age <= 30 * DAY:
                    continue
                safe(p, home)
                action = "delete-expired" if age > 90 * DAY else "compress"
                record("session", p, action, bytes=p.stat().st_size)
                if apply:
                    if age > 90 * DAY:
                        before = stable_stat(p)
                        if now - rollout_activity(p) > 90 * DAY and stable_stat(p) == before:
                            p.unlink()
                    else:
                        archive_one(p, cold / area / p.relative_to(base).with_suffix(".jsonl.gz"), home)
            except (OSError, ValueError) as exc:
                record("session", p, "blocked", error=str(exc))
        # Existing native/compressed archives already satisfy compression. Apply
        # expiry using preserved filesystem activity, without a new dependency.
        packed = [p for p in inventory if p.name.endswith((".jsonl.zst", ".jsonl.gz"))]
        newest_packed = max(packed, key=lambda p: p.stat().st_mtime) if packed else None
        for p in packed:
            if p == newest_packed or now - p.stat().st_mtime <= 90 * DAY:
                continue
            try:
                before = stable_stat(p)
                record("session-native-archive", p, "delete-expired", bytes=p.stat().st_size)
                if apply and stable_stat(p) == before and now - p.stat().st_mtime > 90 * DAY:
                    safe(p, home).unlink()
            except (OSError, ValueError) as exc:
                record("session-native-archive", p, "blocked", error=str(exc))
    if cold.exists():
        for p in tree(cold, home):
            if p.is_file() and p.suffix == ".gz" and now - p.stat().st_mtime > 90 * DAY:
                try:
                    receipt = p.with_name(p.name + ".receipt.json")
                    value = json.loads(receipt.read_text())
                    if value.get("owner") != OWNER or value.get("archive_sha256") != sha256(p):
                        raise ValueError("unowned or modified archive")
                    record("session-archive", p, "delete-expired", bytes=p.stat().st_size)
                    if apply:
                        safe(p, home).unlink()
                        safe(receipt, home).unlink()
                except (OSError, ValueError) as exc:
                    record("session-archive", p, "blocked", error=str(exc))


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
    parser.add_argument("--only", choices=["all", "worktrees", "retention"], default="all")
    args = parser.parse_args()
    home = active_codex_home()
    state = safe(home / "maintenance", home)
    state.mkdir(exist_ok=True)
    lock = state / "run.lock"
    # Kernel lock auto-releases after a crash; the file itself is persistent.
    with lock.open("a+b") as guard:
        try:
            if os.name == "nt":
                import msvcrt
                guard.seek(0)
                if not guard.read(1):
                    guard.write(b"0")
                    guard.flush()
                guard.seek(0)
                msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("Another maintenance run holds the lock")
            return 2
        stamp = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        report = safe(state / "reports" / (stamp + ".jsonl"), home)
        report.parent.mkdir(exist_ok=True)
        failures = []
        with report.open("x", encoding="utf-8") as stream:
            def record(kind, path, action, **details):
                value = {"kind": kind, "path": str(path), "action": action, "apply": args.apply, **details}
                stream.write(json.dumps(value, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                print(json.dumps(value, ensure_ascii=False), flush=True)
                if action == "blocked":
                    failures.append(value)
            now = time.time()
            config = home / "maintenance" / "local.json"
            pinned = {Path.cwd().resolve(), Path(__file__).resolve().parent.parent}
            if config.exists():
                value = json.loads(config.read_text(encoding="utf-8"))
                pinned.update(Path(p).resolve() for p in value.get("pinned_worktrees", []))
            for name, action in [("worktrees", lambda: clean_worktrees(home, args.apply, now, record, pinned)),
                                 ("retention", lambda: (retain_sessions(home, args.apply, now, record), clean_tmp(home, args.apply, now, record)))]:
                if args.only in (name, "all"):
                    try:
                        action()
                    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                        record(name, home, "blocked", error=str(exc))
        print(json.dumps({"report": str(report), "sha256": sha256(report), "blocked": len(failures)}))
        return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
